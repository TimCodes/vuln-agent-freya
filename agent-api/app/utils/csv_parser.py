"""Parse the uploaded vulnerability CSV into grouped VulnerabilityReports.

Expected columns (case-insensitive, extra columns ignored):
  - Name/Package   — e.g. `handlebars (Vulnerable versions: >= 4.0.0, <= 4.7.8)`
  - ID             — CVE id, or `N/A`
  - Location       — `<owner>/<repo>` slug, optionally with ` (Vulnerable manifest path: ...)`
  - Severity       — Critical/High/Moderate/Low/Info
  - Description    — long-form advisory text (truncated before reaching the planner)
  - Unique ID      — fallback id if `ID` is N/A
  - Fixed Version  — OPTIONAL. If present and non-empty/non-"none"/non-"N/A",
                     bypasses the agent's fix-version inference.

Rows are grouped by repo slug; the service then builds one remediation run
per repo. Malformed rows are dropped into `warnings` so the caller can see
what was skipped.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable

from ..core import settings
from ..schemas.models import VulnerabilityReport


_SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "moderate": "moderate",
    "medium": "moderate",
    "low": "low",
    "info": "info",
    "informational": "info",
}

# Tolerated column-name aliases. Match is case-insensitive on the raw header.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "package": ("name/package", "package", "name"),
    "cve": ("id", "cve", "cve id"),
    "location": ("location", "repo", "repository"),
    "severity": ("severity",),
    "description": ("description",),
    "unique_id": ("unique id", "uniqueid", "row id"),
    "fixed_version": ("fixed version", "fixed_version", "fix version"),
}


def _nullish(value: str | None) -> bool:
    if value is None:
        return True
    v = value.strip().strip('"').strip().lower()
    return v in ("", "n/a", "na", "none", "null", "unknown")


def _clean(value: str | None) -> str:
    """Trim whitespace and any outer quotes CSV escaping may have left behind."""
    if value is None:
        return ""
    return value.strip().strip('"').strip()


def _strip_parenthetical(value: str) -> str:
    """`handlebars (Vulnerable versions: ...)` → `handlebars`."""
    value = _clean(value)
    idx = value.find(" (")
    return value[:idx].strip() if idx != -1 else value


def _map_severity(value: str | None) -> str:
    v = _clean(value).lower()
    return _SEVERITY_MAP.get(v, "unknown")


def _build_column_index(fieldnames: Iterable[str]) -> dict[str, str]:
    """Return a mapping of our canonical keys → the CSV header actually used."""
    lowered = {name.lower().strip(): name for name in fieldnames if name}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                resolved[canonical] = lowered[alias]
                break
    return resolved


def parse_vulnerability_csv(
    file_bytes: bytes,
) -> tuple[dict[str, list[VulnerabilityReport]], list[str], int, int]:
    """Parse uploaded CSV bytes.

    Returns `(grouped, warnings, total_rows, skipped_rows)` where `grouped`
    maps repo slug → list[VulnerabilityReport].
    """
    # `utf-8-sig` strips the Excel BOM if present.
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    warnings: list[str] = []
    grouped: dict[str, list[VulnerabilityReport]] = {}
    total = 0
    skipped = 0

    if not reader.fieldnames:
        return grouped, ["csv has no header row"], 0, 0

    cols = _build_column_index(reader.fieldnames)
    for required in ("package", "location"):
        if required not in cols:
            return grouped, [f"csv missing required column: {required}"], 0, 0

    desc_limit = settings.csv_description_max_chars

    for row_num, row in enumerate(reader, start=2):  # row 1 is the header
        total += 1
        slug = _strip_parenthetical(row.get(cols["location"], ""))
        package = _strip_parenthetical(row.get(cols["package"], ""))
        if not slug or not package:
            skipped += 1
            warnings.append(f"row {row_num}: missing slug or package; skipped")
            continue

        cve = _clean(row.get(cols["cve"], "")) if "cve" in cols else ""
        unique_id = _clean(row.get(cols["unique_id"], "")) if "unique_id" in cols else ""
        vuln_id = cve if not _nullish(cve) else unique_id or f"row-{row_num}"

        fixed = None
        if "fixed_version" in cols:
            raw_fixed = row.get(cols["fixed_version"], "")
            if not _nullish(raw_fixed):
                fixed = _clean(raw_fixed)

        description = _clean(row.get(cols["description"], "")) if "description" in cols else ""
        if description and len(description) > desc_limit:
            description = description[:desc_limit] + "…"

        try:
            report = VulnerabilityReport(
                id=vuln_id,
                package=package,
                fixed_version=fixed,
                severity=_map_severity(row.get(cols["severity"], "") if "severity" in cols else ""),
                description=description or None,
            )
        except Exception as e:
            skipped += 1
            warnings.append(f"row {row_num}: validation failed ({e}); skipped")
            continue

        grouped.setdefault(slug, []).append(report)

    return grouped, warnings, total, skipped
