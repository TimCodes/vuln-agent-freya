"""Package update and commit DTOs."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


class PackageUpdateRequest(BaseModel):
    package: str = Field(..., description="npm package name, e.g. 'lodash' or '@scope/name'.")
    version: str = Field(..., description="Target version spec, e.g. '4.17.21' or '^4.17.21'.")
    dev: bool = Field(default=False, description="Install as devDependency.")

    @field_validator("package")
    @classmethod
    def validate_package(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("package cannot be empty")
        # Allowed: lowercase letters, digits, . _ - and optional leading @scope/
        if not re.fullmatch(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", v):
            raise ValueError(f"invalid npm package name: {v}")
        return v

    @field_validator("version")
    @classmethod
    def validate_version(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("version cannot be empty")
        if not re.fullmatch(r"[a-zA-Z0-9._~^><=|\- *]+", v):
            raise ValueError(f"invalid version spec: {v}")
        return v


class PackageUpdateResult(BaseModel):
    package: str
    version: str
    exit_code: int
    stdout: str
    stderr: str
    installed_version: str | None = None


class ResyncManifestRequest(BaseModel):
    # Optional allow-list of package names to re-save. If omitted, every direct
    # dep in package.json is considered. Transitive deps are always skipped.
    packages: list[str] | None = None


class ResyncManifestResult(BaseModel):
    # Direct deps whose package.json version spec was re-saved.
    rewritten: list[str] = Field(default_factory=list)
    # Transitive deps pinned via the `overrides` field at the installed version.
    overrides_added: list[str] = Field(default_factory=list)


class CommitRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    author_name: str = Field(default="VulnFix Bot")
    author_email: str = Field(default="vulnfix-bot@example.com")
    allow_empty: bool = Field(
        default=False,
        description=(
            "Create the commit even when the working tree is unchanged. Used for "
            "informational PRs whose payload is entirely in the PR body."
        ),
    )


class CommitResult(BaseModel):
    commit_sha: str
    files_changed: list[str]
