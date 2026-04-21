"""
Search Agent

Multi-source provider search with intelligent deduplication using 3-tier parallel architecture.

Part of the 7-step provider search workflow (Step 3):
1. LanguageValidator: Validates English input
2. ParsingAgent: Extracts structured provider info
3. SearchAgent (this agent): Queries 3 sources in PARALLEL with deduplication
4. Deduplication: Priority-based (Registry > Oracle > CSV)
5. ScoringAgent: Ranks candidates by match quality
6. ExplanationLLM: Generates natural language explanations
7. Response: Returns best match + top matches

This agent orchestrates PARALLEL queries to 3 data sources and intelligently
deduplicates results to maximize data quality while minimizing search time.

Data Sources (3-Tier Parallel Architecture - ALL queried simultaneously):
1. **NPI Registry** (npi_registry_client): CMS official API
   - Real-time provider data
   - External API with circuit breaker and retry logic
   - ~2s typical response (with retry/backoff)
   - Highest priority in deduplication (Priority 3)

2. **Oracle NPPES** (oracle_nppes_client): Internal database
   - Primary internal source with 53K+ providers
   - Connection pooling for 5x performance improvement
   - ~200ms typical response
   - Medium priority in deduplication (Priority 2)

3. **CSV NPPES** (csv_nppes_client): Static file
   - Parallel query alongside Oracle (not fallback)
   - In-memory pandas DataFrame with 53K+ providers
   - ~50-100ms query time (after initial load)
   - Lowest priority in deduplication (Priority 1)

Query Strategy: ALL 3 SOURCES IN PARALLEL
- Registry query: Starts immediately
- Oracle query: Starts immediately (parallel)
- CSV query: Starts immediately (parallel)
- Results combined after all complete

Deduplication Priority:
When same NPI found in multiple sources, keeps highest priority:
NPPES_REGISTRY (priority 3) > NPPES_ORACLE (priority 2) > NPPES_CSV (priority 1)

Performance:
- Total time: ~2s (limited by slowest source, typically Registry)
- Registry: ~2s (external API with retry)
- Oracle: ~200ms (connection pooling)
- CSV: ~50-100ms (in-memory filtering)
- Deduplication: <10ms (hash-based NPI lookup)

Integration:
Called by ProviderSearchService.search_providers() with StructuredQuery from ParsingAgent.
Returns deduplicated candidate list for ScoringAgent ranking.
"""
from typing import List, Dict, Any
from provider_search_api.core.logging_config import get_logger
from provider_search_api.schemas.models import StructuredQuery
from provider_search_api.clients.npi_registry_client import npi_registry_client
# from provider_search_api.clients.oracle_nppes_client import oracle_nppes_client
# from provider_search_api.clients.csv_nppes_client import csv_nppes_client  # DISABLED: Using only NPI API + Semantic Search

logger = get_logger("provider_search.search_agent")


class SearchAgent:
    """
    Orchestrates 3-tier PARALLEL provider search with intelligent deduplication (Step 3 of 7).
    
    Role in Provider Search Workflow:
    After ParsingAgent extracts structured query, this agent queries ALL 3 data sources
    IN PARALLEL and deduplicates results to provide high-quality candidates.
    
    3-Tier Parallel Architecture:
    ALL 3 sources queried simultaneously (NO fallback, NO conditional logic):
    1. NPI Registry: Real-time CMS data via external API (~2s with retry)
    2. Oracle NPPES: Primary internal DB with 53K+ providers (~200ms with pooling)
    3. CSV NPPES: Static file with 53K+ providers (~50-100ms in-memory)
    
    Deduplication Logic:
    When same NPI appears in multiple sources, keeps highest priority version:
    - NPPES_REGISTRY: Priority 3 (most authoritative, real-time CMS data)
    - NPPES_ORACLE: Priority 2 (internal DB, regularly updated)
    - NPPES_CSV: Priority 1 (static file, snapshot)
    
    Source Field Addition:
    Adds "source" field to each provider dictionary:
    - "NPPES_REGISTRY": From CMS NPI Registry API
    - "NPPES_ORACLE": From Oracle NPPES database
    - "NPPES_CSV": From CSV file
    
    Example Flow:
    1. StructuredQuery: {"last_name": "Smith", "state": "TX", "taxonomy": "Internal Medicine"}
    2. Query ALL 3 in parallel:
       - NPI Registry: 20 results (source=NPPES_REGISTRY)
       - Oracle NPPES: 100 results (source=NPPES_ORACLE)
       - CSV NPPES: 5 results (source=NPPES_CSV)
    3. Combine: 125 total results
    4. Deduplicate: 114 unique providers (11 duplicates removed, kept highest priority)
    5. Return to ScoringAgent for ranking
    
    Performance Characteristics:
    - Total time: ~2s (limited by slowest source, typically Registry)
    - Registry query: ~2s (external API with 3 retries, exponential backoff)
    - Oracle query: ~200ms (connection pooling)
    - CSV query: ~50-100ms (in-memory DataFrame filtering)
    - Deduplication: <10ms (hash-based NPI lookup)
    
    Integration:
    Called by ProviderSearchService in Step 3:
    - Input: StructuredQuery from ParsingAgent
    - Output: List[Dict] of deduplicated candidates for ScoringAgent
    
    Flow: StructuredQuery → search() → 3 parallel queries → Combine → Deduplicate → Candidates
    """
    
    def build_registry_params(self, query: StructuredQuery) -> Dict[str, Any]:
        """
        Build query parameters for NPI Registry API from StructuredQuery.
        
        Maps StructuredQuery fields to NPI Registry API parameter names:
        - query.npi → params["number"]
        - query.first_name → params["first_name"]
        - query.last_name → params["last_name"]
        - query.city → params["city"]
        - query.state → params["state"]
        - query.zipcode → params["postal_code"] (5 digits only)
        - query.taxonomy → params["taxonomy_description"]
        - query.organization_name → params["organization_name"]
        
        Args:
            query: StructuredQuery from ParsingAgent
                - May contain None values (extracted from free text)
                - Example: {"last_name": "Smith", "state": "TX", "taxonomy": "Cardiology"}
        
        Returns:
            Dict[str, Any]: NPI Registry API parameters
                - Only includes non-None query fields
                - Zipcode truncated to 5 digits if provided
                - Empty dict if no valid parameters
        
        Field Validation:
        - Zipcode: Only included if exactly 5 digits (Registry requirement)
        - Other fields: Included as-is if not None
        
        Example Mappings:
        Input: StructuredQuery(last_name="Smith", state="TX")
        Output: {"last_name": "Smith", "state": "TX"}
        
        Input: StructuredQuery(npi="1234567890")
        Output: {"number": "1234567890"}
        
        Input: StructuredQuery(zipcode="78701-1234")
        Output: {"postal_code": "78701"}
        
        Used by query_npi_registry() to construct API request parameters.
        """
        params = {}
        
        if query.npi:
            params["number"] = query.npi
        if query.first_name:
            params["first_name"] = query.first_name
        if query.last_name:
            params["last_name"] = query.last_name
        if query.first_name or query.last_name:
            params["name_purpose"] = "Provider"
        if query.city:
            params["city"] = query.city
        if query.state:
            params["state"] = query.state
        if query.zipcode:
            # Accept both 5-digit (33134) and 9-digit ZIP+4 (331342049) formats
            # NPI Registry will use 9-digit codes for exact matching per documentation
            params["postal_code"] = query.zipcode
        if query.taxonomy:
            params["taxonomy_description"] = query.taxonomy
        if query.organization_name:
            params["organization_name"] = query.organization_name
        
        # Always search PRIMARY practice location address (not mailing address)
        # Per NPI Registry docs: addresses[0] = Primary, addresses[1] = Mailing
        # Set this whenever any location field is provided (city, state, zipcode)
        if query.city or query.state or query.zipcode:
            params["address_purpose"] = "PRIMARY"
        
        return params
    
    async def query_npi_registry(self, query: StructuredQuery) -> List[Dict[str, Any]]:
        """
        Query CMS NPI Registry API for real-time provider data (external source).
        
        Queries the official CMS NPPES NPI Registry via npi_registry_client with:
        - Circuit breaker pattern for fault tolerance
        - Retry logic with exponential backoff (3 attempts)
        - Connection pooling for efficiency
        - 60s timeout per request
        
        Args:
            query: StructuredQuery from ParsingAgent
                - Example: {"last_name": "Smith", "state": "TX", "taxonomy": "Cardiology"}
        
        Returns:
            List[Dict[str, Any]]: Provider results with source="NPPES_REGISTRY"
                - Empty list if no parameters provided
                - Empty list if no matches found
                - Each dict contains nested structure:
                  - number: NPI
                  - basic: {first_name, last_name, organization_name}
                  - addresses: [{city, state, postal_code, ...}]
                  - taxonomies: [{desc, code, primary}]
                  - source: "NPPES_REGISTRY" (added by this method)
        
        Parameter Building:
        1. Calls build_registry_params() to map StructuredQuery to API params
        2. If no params: Logs warning, returns empty list
        3. Passes params to npi_registry_client.search_providers()
        
        Source Field Addition:
        Adds source="NPPES_REGISTRY" to each result for:
        - Deduplication priority (highest: Registry > Oracle > CSV)
        - Response tracking (users know data origin)
        - Monitoring and debugging
        
        Error Handling:
        - Circuit breaker open: Returns empty list (client handles logging)
        - API timeout: Returns partial results or empty list (client retries)
        - HTTP errors: Returns empty list (client handles logging and retry)
        - No exceptions thrown to caller (graceful degradation)
        
        Performance:
        - Typical: ~2s (includes retry/backoff on transient failures)
        - Best case: ~800ms (immediate success)
        - Worst case: ~60s (timeout after 3 retries)
        - Circuit breaker open: <1ms (immediate return)
        
        Data Format Example:
        {
          "number": "1234567890",
          "basic": {"first_name": "John", "last_name": "Smith"},
          "addresses": [{"city": "Austin", "state": "TX"}],
          "taxonomies": [{"desc": "Internal Medicine"}],
          "source": "NPPES_REGISTRY"
        }
        
        Called by search() for parallel multi-source querying.
        """
        params = self.build_registry_params(query)
        if not params:
            logger.warning("No query parameters provided for registry search")
            return []
        
        results = await npi_registry_client.search_providers(params)
        
        # Post-filter results by address and name to ensure precise matches
        # The Registry API limitations:
        # 1. No address support - searches only by city + state
        # 2. May return matches where name only appears in 'other_names'
        # We filter to ensure accurate results match the user's request.
        filtered = []
        query_last = (query.last_name or "").strip().lower() if query.last_name else None
        query_first = (query.first_name or "").strip().lower() if query.first_name else None
        query_address = (query.address or "").strip().lower() if query.address else None
        
        for result in results:
            # If NPI was requested, keep regardless of other fields
            if query.npi:
                filtered.append(result)
                continue
            
            # Address filtering (critical for accurate results)
            # Registry API doesn't support address, so we post-filter here
            if query_address:
                addresses = result.get("addresses", [])
                address_match_found = False
                
                for addr in addresses:
                    # Check primary practice location (not mailing address)
                    address_purpose = (addr.get("address_purpose") or "").upper()
                    if address_purpose not in ["LOCATION", "PRIMARY", ""]:
                        continue
                    
                    # Get full address (address_1 + address_2)
                    addr_line_1 = (addr.get("address_1") or "").strip().lower()
                    addr_line_2 = (addr.get("address_2") or "").strip().lower()
                    full_address = f"{addr_line_1} {addr_line_2}".strip()
                    
                    # Partial match: query address must be contained in provider address
                    # Example: "500 S 7TH AVE" matches "500 S 7TH AVE STE A"
                    if query_address in full_address:
                        address_match_found = True
                        break
                
                if not address_match_found:
                    # Skip this result - address doesn't match
                    continue
            
            # If organization name was requested, keep all organization results
            # (don't apply individual provider name filtering)
            if query.organization_name:
                filtered.append(result)
                continue
            
            # Only apply name filtering for individual provider searches
            # (when first_name or last_name was specified in query)
            if query_last or query_first:
                basic = result.get("basic", {})
                prov_last = (basic.get("last_name") or "").strip().lower()
                prov_first = (basic.get("first_name") or "").strip().lower()
                
                # If query specified last name, require it to be substring of primary last name
                if query_last and query_last not in prov_last:
                    # Skip this result (likely matched only on other_names)
                    continue
                
                # If query specified first name, require it to be substring of primary first name
                if query_first and query_first not in prov_first:
                    # Skip this result (likely matched only on other_names)
                    continue
            
            filtered.append(result)
        
        # Add source field
        for result in filtered:
            result["source"] = "NPPES_REGISTRY"
        
        # Log filtering results
        if query_address:
            logger.info(f"Registry returned {len(filtered)} results after address + name filtering (raw: {len(results)}, address: '{query.address}')")
        else:
            logger.info(f"Registry returned {len(filtered)} results after name-based filtering (raw: {len(results)})")
        return filtered
    
    # def query_nppes_oracle(self, query: StructuredQuery) -> List[Dict[str, Any]]:
        """
        Query Oracle NPPES database for provider data (internal source).
        
        Queries internal Oracle database via oracle_nppes_client with:
        - Connection pooling for 5x performance improvement
        - 53K+ provider records
        - TYPE conversion (NUMBER → string for Pydantic compatibility)
        - 18-column schema with flat structure
        
        Args:
            query: StructuredQuery from ParsingAgent
                - Example: {"last_name": "Smith", "state": "TX", "taxonomy": "Internal Medicine"}
        
        Returns:
            List[Dict[str, Any]]: Provider results with source="NPPES_ORACLE"
                - Empty list if no parameters provided
                - Empty list if no matches found
                - Each dict contains flat structure:
                  - npi: NPI number (string)
                  - first_name, last_name, organization_name
                  - address, city, state, zipcode
                  - taxonomy: Specialty description
                  - source: "NPPES_ORACLE" (added by this method)
        
        Parameter Building:
        1. Maps StructuredQuery fields to Oracle column names
        2. Truncates zipcode to 5 digits (matches DB format)
        3. If no params: Logs warning, returns empty list
        
        Source Field Addition:
        Adds source="NPPES_ORACLE" to each result for:
        - Deduplication priority (Registry > Oracle)
        - Response tracking (users know data origin)
        - Monitoring and debugging
        
        Error Handling:
        - Oracle query failure: Logs error, returns empty list
        - Connection pool error: Logs error, returns empty list
        - No exceptions thrown to caller (graceful degradation)
        
        Performance:
        - Typical: ~200ms (with connection pooling)
        - Without pooling: ~1s (connection overhead)
        - Improvement: 5x faster with pooling
        - Concurrent queries: Shared pool handles efficiently
        
        Data Format Example:
        {
          "npi": "1234567890",
          "first_name": "John",
          "last_name": "Smith",
          "city": "Austin",
          "state": "TX",
          "taxonomy": "Internal Medicine",
          "source": "NPPES_ORACLE"
        }
        
        Database Schema:
        18 columns including: NPI, name fields, address fields, taxonomy,
        entity_type, practice_location_address, mailing_address, etc.
        All NUMBER fields converted to strings for Pydantic validation.
        
        Called by search() for parallel multi-source querying.
        """
        try:
            # Build search parameters
            search_params = {}
            if query.npi:
                search_params["npi"] = query.npi
            if query.first_name:
                search_params["first_name"] = query.first_name
            if query.last_name:
                search_params["last_name"] = query.last_name
            if query.organization_name:
                search_params["organization_name"] = query.organization_name
            if query.city:
                search_params["city"] = query.city
            if query.state:
                search_params["state"] = query.state
            if query.zipcode:
                search_params["zipcode"] = query.zipcode[:5]
            if query.taxonomy:
                search_params["taxonomy"] = query.taxonomy
            if query.address:
                search_params["address"] = query.address
            
            if not search_params:
                logger.warning("No search parameters provided for Oracle query")
                return []
            
            # Query Oracle
            results = oracle_nppes_client.search_providers(search_params)
            
            # Add source field
            for result in results:
                result["source"] = "NPPES_ORACLE"
            
            logger.info(f"Oracle returned {len(results)} results")
            return results
        
        except Exception as e:
            logger.error(f"❌ Oracle query failed: {e}")
            return []
    
    def query_nppes_csv(self, query: StructuredQuery) -> List[Dict[str, Any]]:
        """
        Query CSV NPPES file for provider data (fallback source).
        
        **DISABLED**: This method is currently disabled to use only NPI API + Semantic Search.
        Returns empty list.
        """
        logger.info("CSV NPPES query DISABLED - using only NPI Registry + Semantic Search")
        return []
        
        # DISABLED CODE BELOW - Commented out to use only NPI API + Semantic Search
        # try:
        #     # Build search parameters
        #     search_params = {}
        #     if query.npi:
        #         search_params["npi"] = query.npi
        #     if query.first_name:
        #         search_params["first_name"] = query.first_name
        #     if query.last_name:
        #         search_params["last_name"] = query.last_name
        #     if query.organization_name:
        #         search_params["organization_name"] = query.organization_name
        #     if query.city:
        #         search_params["city"] = query.city
        #     if query.state:
        #         search_params["state"] = query.state
        #     if query.zipcode:
        #         search_params["zipcode"] = query.zipcode[:5]
        #     if query.taxonomy:
        #         search_params["taxonomy"] = query.taxonomy
        #     
        #     if not search_params:
        #         logger.warning("No search parameters provided for CSV query")
        #         return []
        #     
        #     # Query CSV
        #     results = csv_nppes_client.search(search_params)
        #     
        #     # Add source field (if not already added by client)
        #     for result in results:
        #         if "source" not in result:
        #             result["source"] = "NPPES_CSV"
        #     
        #     logger.info(f"CSV returned {len(results)} results")
        #     return results
        # 
        # except Exception as e:
        #     logger.error(f"❌ CSV query failed: {e}")
        #     return []
    
    def deduplicate_providers(self, providers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Remove duplicate providers based on NPI with intelligent 3-tier source prioritization.
        
        When the same provider (identified by NPI) appears in multiple data sources,
        keeps the version from the highest-priority source to maximize data quality.
        
        3-Tier Priority Ranking (higher = better):
        - NPPES_REGISTRY: Priority 3 (CMS official, real-time, most authoritative)
        - NPPES_ORACLE: Priority 2 (Internal DB, regularly updated, reliable)
        - NPPES_CSV: Priority 1 (Static file, emergency backup, lowest priority)
        - UNKNOWN: Priority 0 (Missing source field, fallback)
        
        Args:
            providers: Combined list from all 3 sources (Registry + Oracle + CSV)
                - May contain duplicate NPIs from different sources
                - Example: NPI 1234567890 in Registry, Oracle, and CSV
        
        Returns:
            List[Dict[str, Any]]: Deduplicated provider list
                - Each NPI appears only once
                - Highest-priority version kept for duplicates
                - Providers without NPI kept as-is (not deduplicated)
        
        Deduplication Algorithm:
        1. Initialize empty seen_npis dict and deduplicated list
        2. For each provider:
           a. Extract NPI (handles both "number" and "npi" field names)
           b. If no NPI: Add to deduplicated list immediately (can't deduplicate)
           c. If NPI not seen: Add to seen_npis dict
           d. If NPI already seen: Compare source priorities
              - If new source higher priority: Replace in seen_npis
              - If new source lower/equal priority: Keep existing
        3. Add all unique providers from seen_npis to deduplicated list
        4. Log deduplication statistics
        
        NPI Field Handling:
        - NPI Registry format: provider["number"]
        - Oracle/CSV format: provider["npi"]
        - Checks both field names: provider.get("number") or provider.get("npi")
        
        Example Scenario:
        Input:
        - Registry: NPI 1234567890 (priority 3), name="John A. Smith"
        - Oracle: NPI 1234567890 (priority 2), name="John Smith"
        - CSV: NPI 1234567890 (priority 1), name="J Smith"
        - Oracle: NPI 9876543210 (priority 2), name="Jane Doe"
        
        Output:
        - NPI 1234567890 from Registry (kept, highest priority)
        - NPI 9876543210 from Oracle (kept, unique NPI)
        - Total: 2 providers (2 duplicates removed from Oracle and CSV)
        
        Logging:
        Info log: "Deduplicated {original_count} providers to {final_count}"
        Example: "Deduplicated 45 providers to 30" (15 duplicates removed)
        
        Performance:
        - Time complexity: O(n) where n = number of providers
        - Space complexity: O(m) where m = number of unique NPIs
        - Hash-based lookup: <10ms for typical result sets (30-50 providers)
        
        Quality Impact:
        - Registry data preferred: Most current, authoritative
        - Oracle data secondary: Reliable internal source
        - CSV data tertiary: Emergency backup only
        
        Called by search() after combining all source results.
        """
        seen_npis = {}
        deduplicated = []
        
        # 3-tier source priority (higher = better)
        source_priority = {
            "NPPES_REGISTRY": 3,
            # "NPPES_ORACLE": 2,
            "NPPES_SEMANTIC_SEARCH": 1,
            "NPPES_CSV": 0,
            "UNKNOWN": 0
        }
        
        # First pass: collect all providers by NPI
        for provider in providers:
            npi = provider.get("number") or provider.get("npi")
            if not npi:
                # No NPI, keep it
                deduplicated.append(provider)
                continue
            
            source = provider.get("source", "UNKNOWN")
            
            if npi not in seen_npis:
                seen_npis[npi] = provider
            else:
                # Duplicate NPI - prefer higher priority source
                existing_source = seen_npis[npi].get("source", "UNKNOWN")
                if source_priority.get(source, 0) > source_priority.get(existing_source, 0):
                    seen_npis[npi] = provider
        
        # Add unique providers
        deduplicated.extend(seen_npis.values())
        
        logger.info(f"Deduplicated {len(providers)} providers to {len(deduplicated)}")
        return deduplicated
    
    async def search(self, query: StructuredQuery) -> List[Dict[str, Any]]:
        """
        Search for providers across all 3 tiers in parallel and return deduplicated candidates (Step 3).
        
        This is the main method called by ProviderSearchService to query 3 data sources
        IN PARALLEL and combine results with intelligent deduplication.
        
        Args:
            query: StructuredQuery from ParsingAgent (Step 2)
                - Contains: npi, first_name, last_name, city, state, taxonomy, etc.
                - Example: {"last_name": "Smith", "state": "TX", "taxonomy": "Cardiology"}
        
        Returns:
            List[Dict[str, Any]]: Deduplicated provider candidates
                - Each provider has "source" field (NPPES_REGISTRY, NPPES_ORACLE, or NPPES_CSV)
                - Duplicates removed with priority: Registry > Oracle > CSV
                - Ready for ScoringAgent ranking (Step 4)
        
        3-Tier Parallel Architecture:
        All 3 sources queried simultaneously (NO fallback):
        1. Query NPI Registry (external CMS API, ~2s with retry)
        2. Query Oracle NPPES (internal DB, ~200ms)
        3. Query CSV NPPES (static file, ~50-100ms)
        
        All queries run in parallel - results combined regardless of individual failures.
        
        Result Combination:
        - all_results = registry_results + oracle_results + csv_results
        - May contain duplicate NPIs from different sources
        - Source field tracks origin for each provider
        
        Deduplication:
        Calls deduplicate_providers() with 3-tier priority-based logic:
        - Same NPI in multiple sources: Keep highest priority version
        - NPPES_REGISTRY (priority 3) > NPPES_ORACLE (priority 2) > NPPES_CSV (priority 1)
        - Ensures best data quality for downstream processing
        
        Logging:
        Info log with source breakdown:
        "Total results before deduplication: 120 (Registry: 20, Oracle: 100, CSV: 0)"
        "Deduplicated 120 providers to 114" (from deduplicate_providers)
        
        Example Flow (All Sources Successful):
        Input: StructuredQuery(last_name="Smith", state="TX")
        
        Step 1: Query All 3 Sources in Parallel
        - Registry: 20 providers with source="NPPES_REGISTRY"
        - Oracle: 100 providers with source="NPPES_ORACLE"
        - CSV: 5 providers with source="NPPES_CSV"
        
        Step 2: Combine Results
        - all_results: 125 providers (20 + 100 + 5)
        
        Step 3: Deduplicate
        - Found 11 duplicate NPIs across sources
        - Kept highest priority versions (Registry > Oracle > CSV)
        - Final: 114 unique providers
        
        Example Flow (One Source Fails):
        Input: StructuredQuery(last_name="Smith", state="TX")
        
        Step 1: Query All 3 Sources in Parallel
        - Registry: 20 providers
        - Oracle: 100 providers
        - CSV: 0 providers (query failed, but others succeeded)
        
        Step 2: Combine Results
        - all_results: 120 providers (20 + 100 + 0)
        
        Step 3: Deduplicate
        - Found 6 duplicate NPIs
        - Final: 114 unique providers
        
        Error Handling:
        - Registry failure: Returns Oracle + CSV results (logged by client)
        - Oracle failure: Returns Registry + CSV results
        - CSV failure: Returns Registry + Oracle results
        - All sources fail: Returns empty list (handled by service layer)
        - No exceptions thrown (graceful degradation)
        
        Performance:
        - Parallel queries: ~2s total (limited by slowest source, typically Registry)
        - Oracle: ~200ms (connection pooling)
        - CSV: ~50-100ms (in-memory filtering)
        - Registry: ~2s (with circuit breaker, retry, backoff)
        - Deduplication: <10ms (hash-based)
        
        Integration:
        Called by ProviderSearchService.search_providers() in Step 3:
        1. LanguageValidator validates English (Step 1)
        2. ParsingAgent extracts StructuredQuery (Step 2)
        3. SearchAgent.search() queries 3-tier parallel (Step 3) ← THIS METHOD
        4. ScoringAgent ranks candidates (Step 4)
        5. ExplanationLLM generates explanations (Step 5)
        6. Response formatting (Step 6)
        """
        # Query NPI Registry only (Oracle and CSV disabled)
        registry_results = await self.query_npi_registry(query)
        # oracle_results = self.query_nppes_oracle(query)  # DISABLED
        # csv_results = self.query_nppes_csv(query)  # DISABLED
        
        # Use only registry results
        all_results = registry_results
        
        logger.info(
            f"Total results before deduplication: {len(all_results)} "
            f"(Registry: {len(registry_results)})"
        )
        
        # Deduplicate (though with only one source, minimal duplicates expected)
        deduplicated = self.deduplicate_providers(all_results)
        
        return deduplicated


# Singleton instance for use throughout the application
# Used by ProviderSearchService in Step 3 of 7-step workflow
# Orchestrates 3-tier PARALLEL provider search with intelligent deduplication
# Queries ALL 3 sources simultaneously: NPI Registry + Oracle NPPES + CSV NPPES
# Deduplication priority: Registry (3) > Oracle (2) > CSV (1)
search_agent = SearchAgent()