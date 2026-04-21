"""Request/response models for the Agent API."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


# ---------- incoming vulnerability report ----------

class VulnerabilityReport(BaseModel):
    """A single vulnerability as reported by an upstream scanner.

    This is the agent's external contract — the scanner / ticket system posts
    one or more of these in the remediation request.
    """
    id: str = Field(..., description="Identifier used by the reporter (e.g. CVE-2024-1234, GHSA-xxxx).")
    package: str = Field(..., description="Affected npm package name.")
    current_version: str | None = Field(default=None, description="Installed version if known.")
    fixed_version: str | None = Field(
        default=None,
        description="Minimum version that remediates the vulnerability, if known.",
    )
    severity: Literal["low", "moderate", "high", "critical", "info", "unknown"] = "unknown"
    description: str | None = None


class RemediateRequest(BaseModel):
    repo_url: str
    branch: str = "main"
    vulnerabilities: list[VulnerabilityReport] = Field(default_factory=list)


# ---------- fix plan (internal + returned) ----------

class FixAction(BaseModel):
    """A single action the plan will take."""
    type: Literal["npm_audit_fix", "package_update"]
    # For package_update:
    package: str | None = None
    target_version: str | None = None
    reason: str = ""
    # Which reported vuln IDs (and/or npm audit advisory IDs) this addresses.
    addresses: list[str] = Field(default_factory=list)


class AppliedFix(BaseModel):
    action: FixAction
    success: bool
    details: str = ""
    # Human-readable suggestion surfaced in the PR body when `success` is
    # False. Populated by the executor when it has a concrete next step
    # (e.g. a published version to try manually); empty otherwise.
    recommendation: str = ""


# ---------- final response ----------

class RemediationResult(BaseModel):
    repo_url: str
    branch: str
    workspace_id: str | None
    applied_fixes: list[AppliedFix] = Field(default_factory=list)
    pr_url: str | None = None
    summary: str = ""
    errors: list[str] = Field(default_factory=list)


# ---------- batch (CSV upload) ----------

class BatchRepoResult(BaseModel):
    """Per-repo entry in a batch remediation response."""
    repo_slug: str
    repo_url: str
    vulnerabilities_reported: int
    result: RemediationResult | None = None
    error: str | None = None


class BatchRemediationResult(BaseModel):
    total_rows: int
    rows_skipped: int = 0
    repos_processed: int
    repos_succeeded: int
    repos_failed: int
    results: list[BatchRepoResult] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)
