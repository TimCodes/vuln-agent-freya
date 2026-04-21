"""Batch remediation — run one remediation graph per repo in a CSV upload.

Flow:
  1. Parse the CSV into `{repo_slug: [VulnerabilityReport, ...]}`.
  2. For each repo, build a `RemediateRequest` (slug → full git URL).
  3. Fan out across repos with a semaphore bounded by
     `settings.batch_remediation_concurrency`. Repos beyond the cap queue
     up and start as soon as a slot frees.
  4. Collect per-repo `BatchRepoResult`s — failures are recorded, not
     raised — and return a single `BatchRemediationResult`.

Per-repo work is delegated to the existing `RemediationService`, so the
graph and agents don't change.
"""
from __future__ import annotations

import asyncio

from ..core import get_logger, settings
from ..schemas.models import (
    BatchRemediationResult,
    BatchRepoResult,
    RemediateRequest,
)
from ..utils import parse_vulnerability_csv
from .remediation_service import RemediationService, remediation_service

logger = get_logger("agent.batch_service")


class BatchRemediationService:
    """Coordinates fan-out remediation across many repos from one CSV."""

    def __init__(
        self,
        remediation: RemediationService | None = None,
        concurrency: int | None = None,
        github_host: str | None = None,
    ) -> None:
        self._remediation = remediation or remediation_service
        self._concurrency = concurrency or settings.batch_remediation_concurrency
        self._github_host = (github_host or settings.github_host).rstrip("/") + "/"

    def _slug_to_url(self, slug: str) -> str:
        """`acme-corp/test-foo` → `https://github.com/acme-corp/test-foo.git`."""
        slug = slug.strip().strip("/")
        if slug.endswith(".git"):
            return f"{self._github_host}{slug}"
        return f"{self._github_host}{slug}.git"

    async def _remediate_one(
        self,
        slug: str,
        repo_url: str,
        vulns,
        sem: asyncio.Semaphore,
    ) -> BatchRepoResult:
        async with sem:
            logger.info("batch: remediating slug=%s vulns=%d", slug, len(vulns))
            req = RemediateRequest(repo_url=repo_url, branch="main", vulnerabilities=vulns)
            try:
                result = await self._remediation.remediate(req)
                return BatchRepoResult(
                    repo_slug=slug,
                    repo_url=repo_url,
                    vulnerabilities_reported=len(vulns),
                    result=result,
                )
            except Exception as e:
                logger.exception("batch: remediation failed for slug=%s", slug)
                return BatchRepoResult(
                    repo_slug=slug,
                    repo_url=repo_url,
                    vulnerabilities_reported=len(vulns),
                    error=f"remediation failed: {e}",
                )

    async def remediate_batch(self, file_bytes: bytes) -> BatchRemediationResult:
        grouped, warnings, total_rows, skipped = parse_vulnerability_csv(file_bytes)

        if not grouped:
            return BatchRemediationResult(
                total_rows=total_rows,
                rows_skipped=skipped,
                repos_processed=0,
                repos_succeeded=0,
                repos_failed=0,
                parse_warnings=warnings,
            )

        logger.info(
            "batch: parsed rows=%d skipped=%d repos=%d concurrency=%d",
            total_rows, skipped, len(grouped), self._concurrency,
        )

        sem = asyncio.Semaphore(self._concurrency)
        tasks = [
            self._remediate_one(slug, self._slug_to_url(slug), vulns, sem)
            for slug, vulns in grouped.items()
        ]
        results = await asyncio.gather(*tasks)

        succeeded = sum(1 for r in results if r.error is None)
        failed = len(results) - succeeded
        return BatchRemediationResult(
            total_rows=total_rows,
            rows_skipped=skipped,
            repos_processed=len(results),
            repos_succeeded=succeeded,
            repos_failed=failed,
            results=results,
            parse_warnings=warnings,
        )


batch_remediation_service = BatchRemediationService()
