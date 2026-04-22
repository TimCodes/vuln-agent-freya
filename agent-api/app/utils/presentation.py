"""Commit message, PR body, and PR branch-name builders.

Presentation helpers consumed by graph nodes. Code-remediation copy is
derived from a `RemediationReport` so it reflects what actually changed
between audits rather than what the planner intended. Image and
unclassified rows are presented directly from their `VulnerabilityReport`
inputs since no mutation or audit diff exists for them.
"""
from __future__ import annotations

from ..core.config import settings
from ..schemas.models import VulnerabilityReport
from .remediation_report import (
    STATUS_FAILED,
    STATUS_FIXED,
    STATUS_NOT_ATTEMPTED,
    STATUS_STILL_VULNERABLE,
    PackageChange,
    RemediationReport,
)


SEVERITY_ORDER = ["critical", "high", "moderate", "low", "info"]

CVE_LINK_PREFIX = "https://nvd.nist.gov/vuln/detail/"
GHSA_LINK_PREFIX = "https://github.com/advisories/"


def _link_vuln_id(vuln_id: str) -> str:
    """Turn a CVE-/GHSA- id into a markdown link; leave other ids as-is."""
    if vuln_id.upper().startswith("CVE-"):
        return f"[{vuln_id}]({CVE_LINK_PREFIX}{vuln_id})"
    if vuln_id.upper().startswith("GHSA-"):
        return f"[{vuln_id}]({GHSA_LINK_PREFIX}{vuln_id})"
    return vuln_id


def _format_cves(ids: list[str]) -> str:
    return ", ".join(_link_vuln_id(i) for i in ids) if ids else "—"


def _counts_line(counts: dict[str, int]) -> str:
    parts = []
    for sev in SEVERITY_ORDER:
        if counts.get(sev):
            parts.append(f"{counts[sev]} {sev}")
    return ", ".join(parts) if parts else "none"


def commit_message(
    report: RemediationReport,
    *,
    image_vulns: list[VulnerabilityReport] | None = None,
    unclassified_vulns: list[VulnerabilityReport] | None = None,
) -> str:
    """Short, truthful commit message derived from the reconciled report.

    When ``report`` describes no code fixes but ``image_vulns`` /
    ``unclassified_vulns`` are non-empty, emit an informational header —
    this commit is intentionally empty and the payload is in the PR body.
    """
    image_vulns = image_vulns or []
    unclassified_vulns = unclassified_vulns or []
    had_code_work = report.had_any_success or bool(report.package_changes)

    if not had_code_work and (image_vulns or unclassified_vulns):
        header = "VulnFix: informational vulnerability report"
    else:
        header = "VulnFix: automated remediation"
    lines = [header, ""]

    if report.resolved_packages:
        lines.append(
            f"Resolved {len(report.resolved_packages)} package(s): "
            f"{', '.join(report.resolved_packages)}."
        )
    if report.unresolved_packages:
        lines.append(
            f"Remaining vulnerable: {', '.join(report.unresolved_packages)}."
        )
    if had_code_work:
        lines.append("")
        lines.append(
            f"Before: {report.initial_total} vuln(s) ({_counts_line(report.initial_counts)})."
        )
        lines.append(
            f"After:  {report.final_total} vuln(s) ({_counts_line(report.final_counts)})."
        )
    if report.major_upgrades:
        majors = ", ".join(c.package for c in report.major_upgrades)
        lines.append("")
        lines.append(f"Includes major-version upgrade(s): {majors}.")

    if image_vulns:
        lines.append("")
        lines.append(
            f"Image vulnerabilities ({len(image_vulns)}): see PR body — "
            f"rebuild affected base image(s) to remediate."
        )
    if unclassified_vulns:
        lines.append("")
        lines.append(
            f"Unclassified vulnerabilities ({len(unclassified_vulns)}): see PR body — "
            f"manual triage required."
        )
    return "\n".join(lines).rstrip() + "\n"


def _table_row(cells: list[str]) -> str:
    # Escape pipe chars so they don't break the table.
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def _changes_table(changes: list[PackageChange]) -> list[str]:
    if not changes:
        return []
    lines = [
        _table_row(["Package", "Severity", "CVE/GHSA", "Action", "Status", "Notes"]),
        _table_row(["---", "---", "---", "---", "---", "---"]),
    ]
    for c in changes:
        lines.append(_table_row([
            f"`{c.package}`",
            c.severity,
            _format_cves(c.cve_ids),
            f"`{c.action_summary}`" if c.action_summary != "None" else "—",
            c.status,
            c.notes or "—",
        ]))
    return lines


def _major_upgrades_section(report: RemediationReport) -> list[str]:
    if not report.major_upgrades:
        return []
    lines = ["## Major-version upgrades", ""]
    lines.append(
        "`npm audit fix` will not apply upgrades that cross a major-version "
        "boundary because they can introduce breaking changes. The packages "
        "below require explicit intervention:"
    )
    lines.append("")
    for c in report.major_upgrades:
        rec = f" -> `{c.recommended_version}`" if c.recommended_version else ""
        resolved = " (applied automatically by this PR)" if c.status == STATUS_FIXED else " (not applied — review required)"
        lines.append(f"- **`{c.package}`**{rec}{resolved}")
        if c.cve_ids:
            lines.append(f"  - Addresses: {_format_cves(c.cve_ids)}")
    lines.append("")
    lines.append(
        "**Reviewer note:** skim the changelog / release notes for any listed "
        "package before merging — breaking API changes may require callsite updates."
    )
    return lines


def _residual_section(report: RemediationReport) -> list[str]:
    residuals = [
        c for c in report.package_changes
        if c.status in (STATUS_STILL_VULNERABLE, STATUS_NOT_ATTEMPTED)
    ]
    if not residuals:
        return []
    lines = ["## Residual vulnerabilities", ""]
    lines.append(
        "The following package(s) remain vulnerable after this PR and need "
        "manual attention:"
    )
    lines.append("")
    for c in residuals:
        reason = (
            "requires major-version upgrade"
            if c.is_major_bump
            else "no fix available via npm"
        )
        rec = f"; recommended: `{c.recommended_version}`" if c.recommended_version else ""
        lines.append(f"- `{c.package}` ({c.severity}) — {reason}{rec}")
    return lines


def _failed_section(report: RemediationReport) -> list[str]:
    if not report.failed_actions:
        return []
    lines = ["## Failed actions", ""]
    for a in report.failed_actions:
        label = (
            f"npm audit fix"
            if a.action.type == "npm_audit_fix"
            else f"{a.action.package}@{a.action.target_version}"
        )
        detail = (a.details or "").strip().replace("\n", " ")
        if len(detail) > 300:
            detail = detail[:300] + "…"
        lines.append(f"- `{label}` — {detail or 'no details'}")
        recommendation = (a.recommendation or "").strip().replace("\n", " ")
        if recommendation:
            lines.append(f"  - **Suggested fix:** {recommendation}")
    return lines


def _checklist(
    report: RemediationReport, committed_files: list[str] | None = None,
) -> list[str]:
    items = ["- [ ] CI is green"]
    # `npm audit fix` can patch a vulnerability purely in the lockfile when the
    # fix version already satisfies the range declared in package.json, so we
    # only tell the reviewer to expect a package.json change when one actually
    # landed — otherwise the checklist implies a bug that isn't there.
    files = list(committed_files or [])
    has_manifest = any(f == "package.json" or f.endswith("/package.json") for f in files)
    has_lock = any(f == "package-lock.json" or f.endswith("/package-lock.json") for f in files)
    if has_manifest and has_lock:
        items.append("- [ ] `package.json` and `package-lock.json` are both updated")
    elif has_lock:
        items.append(
            "- [ ] `package-lock.json` is updated (package.json unchanged — "
            "fix version already satisfied the declared range)"
        )
    elif has_manifest:
        items.append("- [ ] `package.json` is updated")
    if report.major_upgrades:
        items.append("- [ ] Reviewed release notes for major-version upgrade(s)")
        items.append("- [ ] Local smoke test / test suite covers affected code paths")
    if report.unresolved_packages:
        items.append("- [ ] Triaged remaining vulnerable package(s) listed above")
    return ["## Review checklist", "", *items]


def _group_by_package(
    vulns: list[VulnerabilityReport],
) -> list[tuple[str, list[VulnerabilityReport]]]:
    """Group vulns by package, preserving first-seen order."""
    order: list[str] = []
    buckets: dict[str, list[VulnerabilityReport]] = {}
    for v in vulns:
        if v.package not in buckets:
            buckets[v.package] = []
            order.append(v.package)
        buckets[v.package].append(v)
    return [(pkg, buckets[pkg]) for pkg in order]


def _image_vulns_section(image_vulns: list[VulnerabilityReport]) -> list[str]:
    if not image_vulns:
        return []
    lines = ["## Image vulnerabilities", ""]
    lines.append(
        "The following vulnerabilities live in base-image / OS packages and "
        "cannot be remediated by this pipeline. **Manual action required**: "
        "rebuild the affected container image against an updated base or "
        "install the vendor-patched package at image-build time."
    )
    lines.append("")
    lines.append(_table_row(["Package", "Severity", "CVE/GHSA", "Manifest", "Fix"]))
    lines.append(_table_row(["---", "---", "---", "---", "---"]))
    for pkg, items in _group_by_package(image_vulns):
        severity = items[0].severity
        ids = [v.id for v in items]
        manifest = items[0].manifest_path or "—"
        fixed = next((v.fixed_version for v in items if v.fixed_version), None)
        fix_text = f"`{fixed}`" if fixed else "see advisory"
        lines.append(_table_row([
            f"`{pkg}`",
            severity,
            _format_cves(ids),
            f"`{manifest}`" if manifest != "—" else "—",
            fix_text,
        ]))
    return lines


def _unclassified_vulns_section(
    unclassified_vulns: list[VulnerabilityReport],
) -> list[str]:
    if not unclassified_vulns:
        return []
    lines = ["## Unclassified vulnerabilities — manual intervention required", ""]
    lines.append(
        "The classifier could not confidently determine whether these "
        "vulnerabilities live in application code or in a container image. "
        "**A human reviewer must triage each row** and remediate manually "
        "(update a dependency, rebuild the image, or mark the row as a "
        "false positive in the scanner)."
    )
    lines.append("")
    lines.append(_table_row(["Package", "Severity", "CVE/GHSA", "Manifest", "Fix"]))
    lines.append(_table_row(["---", "---", "---", "---", "---"]))
    for pkg, items in _group_by_package(unclassified_vulns):
        severity = items[0].severity
        ids = [v.id for v in items]
        manifest = items[0].manifest_path or "—"
        fixed = next((v.fixed_version for v in items if v.fixed_version), None)
        fix_text = f"`{fixed}`" if fixed else "—"
        lines.append(_table_row([
            f"`{pkg}`",
            severity,
            _format_cves(ids),
            f"`{manifest}`" if manifest != "—" else "—",
            fix_text,
        ]))
    return lines


def _files_changed_section(committed_files: list[str] | None) -> list[str]:
    files = [f for f in (committed_files or []) if f]
    if not files:
        return []
    lines = ["## Files changed", ""]
    for f in files:
        lines.append(f"- `{f}`")
    return lines


def _informational_checklist(
    image_vulns: list[VulnerabilityReport],
    unclassified_vulns: list[VulnerabilityReport],
) -> list[str]:
    """Checklist for PRs where nothing in the tree changed — only reviewer
    follow-up tasks make sense."""
    items: list[str] = []
    if image_vulns:
        items.append("- [ ] Identified the base image(s) flagged above")
        items.append("- [ ] Rebuilt container image against a patched base / ran vendor update")
    if unclassified_vulns:
        items.append("- [ ] Triaged each unclassified row and chose a remediation path")
    return ["## Review checklist", "", *items] if items else []


def pr_body(
    report: RemediationReport,
    *,
    committed_files: list[str] | None = None,
    image_vulns: list[VulnerabilityReport] | None = None,
    unclassified_vulns: list[VulnerabilityReport] | None = None,
) -> str:
    """Rich PR description derived from the reconciled remediation report
    plus any image / unclassified vulnerabilities that require human
    follow-up.

    Code remediation status comes from the audit diff (authoritative). Image
    and unclassified entries come straight from the scanner report because
    there's no delta to compute — they're informational.
    """
    image_vulns = image_vulns or []
    unclassified_vulns = unclassified_vulns or []
    had_code_work = report.had_any_success or bool(report.package_changes)

    lines: list[str] = []
    if had_code_work:
        lines.append("Automated vulnerability remediation by VulnFix.")
    else:
        lines.append(
            "Informational vulnerability report by VulnFix — no code changes "
            "were applied. See below for the items that need manual attention."
        )
    lines.append("")

    # TL;DR
    resolved_n = len(report.resolved_packages)
    unresolved_n = len(report.unresolved_packages)
    tldr_parts = []
    if resolved_n:
        tldr_parts.append(f"resolved **{resolved_n}** package(s)")
    if unresolved_n:
        tldr_parts.append(f"**{unresolved_n}** still need(s) manual attention")
    if report.major_upgrades:
        tldr_parts.append(f"**{len(report.major_upgrades)}** major-version upgrade(s)")
    if image_vulns:
        tldr_parts.append(f"**{len(image_vulns)}** image vulnerability(ies) to rebuild")
    if unclassified_vulns:
        tldr_parts.append(
            f"**{len(unclassified_vulns)}** unclassified vulnerability(ies) "
            f"requiring manual triage"
        )
    if not tldr_parts:
        tldr_parts.append("no remediation changes were required")
    lines.append("## Summary")
    lines.append("")
    lines.append("This PR " + "; ".join(tldr_parts) + ".")
    lines.append("")

    # Per-package changes table
    changes = _changes_table(report.package_changes)
    if changes:
        lines.append("## Changes by package")
        lines.append("")
        lines.extend(changes)
        lines.append("")

    # Major upgrades explanation
    majors = _major_upgrades_section(report)
    if majors:
        lines.extend(majors)
        lines.append("")

    # Residuals
    residuals = _residual_section(report)
    if residuals:
        lines.extend(residuals)
        lines.append("")

    # Failures
    failed = _failed_section(report)
    if failed:
        lines.extend(failed)
        lines.append("")

    # Image vulnerabilities (informational)
    image_section = _image_vulns_section(image_vulns)
    if image_section:
        lines.extend(image_section)
        lines.append("")

    # Unclassified vulnerabilities (manual triage)
    unclass_section = _unclassified_vulns_section(unclassified_vulns)
    if unclass_section:
        lines.extend(unclass_section)
        lines.append("")

    # Files actually committed (git status --porcelain from the commit step).
    files_section = _files_changed_section(committed_files)
    if files_section:
        lines.extend(files_section)
        lines.append("")

    # Checklist
    if had_code_work:
        lines.extend(_checklist(report, committed_files))
    else:
        lines.extend(_informational_checklist(image_vulns, unclassified_vulns))
    lines.append("")

    lines.append("---")
    if had_code_work:
        lines.append(
            "_Generated by VulnFix. Counts come from `npm audit --json` runs "
            "before and after remediation; status per package is derived from "
            "the audit diff, not from planner intent._"
        )
    else:
        lines.append(
            "_Generated by VulnFix. This PR contains an empty commit so the "
            "informational report has a reviewable target — no files were "
            "modified in the tree._"
        )
    return "\n".join(lines)


def pr_head_branch(workspace_id: str) -> str:
    """Derive the auto-generated PR head branch name from a workspace id."""
    return f"{settings.pr_branch_prefix}{workspace_id[:8]}"
