"""Reduce an `npm audit --json` payload to the fields used by the
agent's planner, summarizer, and response shaping."""
from __future__ import annotations

from typing import Any


def summarize_audit(audit: dict[str, Any] | None) -> dict[str, Any]:
    if not audit:
        return {}
    meta = audit.get("metadata", {}) or {}
    vulns_meta = meta.get("vulnerabilities", {}) or {}
    return {
        "total": sum(v for v in vulns_meta.values() if isinstance(v, int)),
        "by_severity": {k: v for k, v in vulns_meta.items() if isinstance(v, int)},
        "affected_packages": sorted(list((audit.get("vulnerabilities") or {}).keys())),
    }
