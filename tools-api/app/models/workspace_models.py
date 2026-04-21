"""Workspace request/response DTOs."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


_BRANCH_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"
)


class CreateWorkspaceRequest(BaseModel):
    repo_url: str = Field(..., description="Git URL to clone. https:// or git@ supported.")
    branch: str = Field(default="main", description="Branch to check out.")
    depth: int | None = Field(default=1, description="Shallow clone depth. None for full history.")

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("repo_url cannot be empty")
        if any(c in v for c in ["\n", "\r", "\0"]):
            raise ValueError("repo_url contains invalid characters")
        return v

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("branch cannot be empty")
        if not all(c in _BRANCH_CHARS for c in v):
            raise ValueError("branch contains invalid characters")
        return v


class WorkspaceInfo(BaseModel):
    workspace_id: str
    repo_url: str
    branch: str
    path: str
    created_at: datetime
    current_commit: str | None = None
