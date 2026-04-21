"""Remediation report builder.

Reconciles three inputs that each tell a partial truth about what happened
during a run:

  * `initial_audit` — the vulns that existed at the start.
  * `final_audit`   — the vulns that still exist at the end.
  * `applied_fixes` — what the executor *tried* to do (and whether each
                       subprocess returned a non-zero exit).

The planner's `addresses` lists express intent, not outcome. npm can
silently decline to fix a vuln (e.g. when `fixAvailable.isSemVerMajor` is
true and the caller is not using `--force`), so relying on `addresses` for
the PR body caused overclaiming in earlier runs.

`build_report` resolves this by computing what actually changed between
the two audits and bucketing each package into one of four states.
Downstream presenters consume `RemediationReport` rather than reasoning
about the raw inputs themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schemas.models import AppliedFix, VulnerabilityReport


# Status values used in the per-package table.
STATUS_FIXED = "Fixed"
STATUS_STILL_VULNERABLE = "Still vulnerable"
STATUS_FAILED = "Failed"
STATUS_NOT_ATTEMPTED = "Not attempted"


@dataclass
class PackageChange:
    """One row in the per-package changes table."""
    package: str
    severity: str
    cve_ids: list[str]
    action_summary: str
    status: str
    is_major_bump: bool = False
    recommended_version: str | None = None
    notes: str = ""


@dataclass
class RemediationReport:
    """Reconciled view of what actually happened during a run."""
    initial_counts: dict[str, int] = field(default_factory=dict)
    final_counts: dict[str, int] = field(default_factory=dict)
    initial_total: int = 0
    final_total: int = 0
    resolved_packages: list[str] = field(default_factory=list)
    unresolved_packages: list[str] = field(default_factory=list)
    package_changes: list[PackageChange] = field(default_factory=list)
    actual_addresses: list[str] = field(default_factory=list)
    major_upgrades: list[PackageChange] = field(default_factory=list)
    failed_actions: list[AppliedFix] = field(default_factory=list)
    had_any_success: bool = False


def _vuln_names(audit: dict[str, Any] | None) -> set[str]:
    if not audit:
        return set()
    return set((audit.get("vulnerabilities") or {}).keys())


_SEVERITY_KEYS = ("info", "low", "moderate", "high", "critical")


def _counts(audit: dict[str, Any] | None) -> tuple[dict[str, int], int]:
    if not audit:
        return {}, 0
    meta = (audit.get("metadata") or {}).get("vulnerabilities") or {}
    counts = {k: v for k, v in meta.items() if k in _SEVERITY_KEYS and isinstance(v, int)}
    # Prefer npm's reported `total` if present — it matches the severity breakdown.
    total = meta.get("total")
    if not isinstance(total, int):
        total = sum(counts.values())
    return counts, total


def _cves_for_package(reported: list[VulnerabilityReport], package: str) -> list[str]:
    return [v.id for v in reported if v.package == package]


def _severity_for_package(audit: dict[str, Any] | None, package: str) -> str:
    if not audit:
        return "unknown"
    entry = (audit.get("vulnerabilities") or {}).get(package) or {}
    return entry.get("severity") or "unknown"


def _fix_available(audit: dict[str, Any] | None, package: str) -> Any:
    if not audit:
        return None
    entry = (audit.get("vulnerabilities") or {}).get(package) or {}
    return entry.get("fixAvailable")


def build_report(
    initial_audit: dict[str, Any] | None,
    final_audit: dict[str, Any] | None,
    applied_fixes: list[AppliedFix],
    reported: list[VulnerabilityReport],
) -> RemediationReport:
    """Build a reconciled remediation report from the run's state."""
    initial_counts, initial_total = _counts(initial_audit)
    final_counts, final_total = _counts(final_audit)

    initial_pkgs = _vuln_names(initial_audit)
    final_pkgs = _vuln_names(final_audit)
    resolved = sorted(initial_pkgs - final_pkgs)
    unresolved = sorted(initial_pkgs & final_pkgs)

    # Group applied fixes by action type / package for presentation.
    audit_fix_attempts = [a for a in applied_fixes if a.action.type == "npm_audit_fix"]
    pkg_update_by_name = {
        a.action.package: a
        for a in applied_fixes
        if a.action.type == "package_update" and a.action.package
    }

    package_changes: list[PackageChange] = []
    major_upgrades: list[PackageChange] = []

    # Every package that was vulnerable at the start gets a row — whether
    # it got fixed, stayed vulnerable, failed an attempt, or wasn't touched.
    for pkg in sorted(initial_pkgs):
        severity = _severity_for_package(initial_audit, pkg)
        cves = _cves_for_package(reported, pkg)
        fix_info = _fix_available(initial_audit, pkg)
        is_major = False
        recommended = None
        if isinstance(fix_info, dict):
            is_major = bool(fix_info.get("isSemVerMajor"))
            recommended = fix_info.get("version") if isinstance(fix_info.get("version"), str) else None

        applied = pkg_update_by_name.get(pkg)
        action_summary: str
        if applied:
            action_summary = (
                f"npm install {applied.action.package}@{applied.action.target_version}"
            )
        elif audit_fix_attempts:
            action_summary = "npm audit fix"
        else:
            action_summary = "None"

        # Order matters: a failed targeted install is reported as FAILED even
        # if the vuln happened to disappear in the final audit (e.g. because
        # `npm audit fix` also ran and patched it via a different path). The
        # action column names the install command we tried — conflating that
        # with "Fixed" misleads reviewers about which action actually worked.
        if applied and not applied.success:
            status = STATUS_FAILED
        elif pkg not in final_pkgs:
            status = STATUS_FIXED
        elif applied or audit_fix_attempts:
            status = STATUS_STILL_VULNERABLE
        else:
            status = STATUS_NOT_ATTEMPTED

        notes = ""
        if is_major and status != STATUS_FIXED:
            notes = "Requires major-version upgrade"
        elif is_major:
            notes = "Major-version upgrade"

        change = PackageChange(
            package=pkg,
            severity=severity,
            cve_ids=cves,
            action_summary=action_summary,
            status=status,
            is_major_bump=is_major,
            recommended_version=recommended,
            notes=notes,
        )
        package_changes.append(change)
        if is_major:
            major_upgrades.append(change)

    # Only CVEs for packages that actually disappeared are real addresses.
    actual_addresses: list[str] = []
    for c in package_changes:
        if c.status == STATUS_FIXED:
            actual_addresses.extend(c.cve_ids)
    # De-dupe while preserving order.
    seen: set[str] = set()
    dedup_addresses = [x for x in actual_addresses if not (x in seen or seen.add(x))]

    failed_actions = [a for a in applied_fixes if not a.success]
    had_any_success = any(a.success for a in applied_fixes) or bool(resolved)

    return RemediationReport(
        initial_counts=initial_counts,
        final_counts=final_counts,
        initial_total=initial_total,
        final_total=final_total,
        resolved_packages=resolved,
        unresolved_packages=unresolved,
        package_changes=package_changes,
        actual_addresses=dedup_addresses,
        major_upgrades=major_upgrades,
        failed_actions=failed_actions,
        had_any_success=had_any_success,
    )
