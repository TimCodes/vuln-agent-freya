"""Pull request DTOs."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


_BRANCH_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-"
)


def _validate_branch(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("branch cannot be empty")
    if not all(c in _BRANCH_CHARS for c in v):
        raise ValueError("branch contains invalid characters")
    return v


class PullRequestRequest(BaseModel):
    base_branch: str = Field(..., description="Branch to merge into (e.g. 'main').")
    head_branch: str = Field(..., description="New branch name to create and push.")
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(default="", max_length=50_000)

    @field_validator("base_branch", "head_branch")
    @classmethod
    def _branch(cls, v: str) -> str:
        return _validate_branch(v)


class PullRequestResult(BaseModel):
    url: str
    number: int
    head_branch: str
    base_branch: str


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
