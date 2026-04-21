"""npm request/response DTOs."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class NpmAuditResult(BaseModel):
    """Raw-ish shape of `npm audit --json`. We preserve the interesting bits."""

    vulnerabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_exit_code: int
    stderr: str = ""

    @classmethod
    def from_raw(cls, raw: dict) -> "NpmAuditResult":
        """Build from the dict returned by `npm_service.audit()`.

        The service tags exit code and stderr onto the parsed JSON via
        `_exit_code` / `_stderr` so the caller doesn't have to carry a
        separate CompletedProcess around.
        """
        exit_code = raw.pop("_exit_code", 0)
        stderr = raw.pop("_stderr", "")
        return cls(
            vulnerabilities=raw.get("vulnerabilities", {}) or {},
            metadata=raw.get("metadata", {}) or {},
            raw_exit_code=exit_code,
            stderr=stderr,
        )


class NpmAuditFixResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    audit_after: NpmAuditResult | None = None
