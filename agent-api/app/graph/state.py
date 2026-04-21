"""LangGraph state object for the remediation workflow.

LangGraph uses a TypedDict-style state that flows through each node. Each
node returns a partial dict which is merged into the state. We deliberately
keep the state flat and serializable so it could be persisted (e.g. to
Postgres via LangGraph's checkpointer) later.
"""
from __future__ import annotations

from typing import Any, TypedDict

from ..schemas.models import AppliedFix, FixAction, VulnerabilityReport


class AgentState(TypedDict, total=False):
    # --- inputs ---
    repo_url: str
    branch: str
    reported_vulnerabilities: list[VulnerabilityReport]

    # --- produced by workspace creation ---
    workspace_id: str | None

    # --- produced by initial audit ---
    initial_audit: dict[str, Any] | None

    # --- produced by planning ---
    fix_plan: list[FixAction]

    # --- produced by execution ---
    applied_fixes: list[AppliedFix]

    # --- produced by verification ---
    final_audit: dict[str, Any] | None

    # --- produced by commit step ---
    commit_sha: str | None
    # Paths that the commit actually touched, as reported by the tools-api
    # `git status --porcelain` check. Surfaced in the PR body so reviewers see
    # what really changed (e.g. lockfile-only updates when an audit-fix patch
    # bump stays within the existing package.json range).
    committed_files: list[str]

    # --- produced by open-pr step ---
    pr_url: str | None
    pr_number: int | None
    pr_branch: str | None

    # --- produced by summary node ---
    summary: str

    # --- collected throughout ---
    errors: list[str]
