"""Commit-changes workflow.

Steps:
  1. Check whether there are any changed files in the workspace.
     If not, raise `NoChangesToCommitError` (router maps to HTTP 400).
  2. `git add -A` + `git commit -m <message>` with inline author identity.
"""
from __future__ import annotations

from ..models.package_models import CommitResult
from ..services import git_service, workspace_service


class NoChangesToCommitError(Exception):
    pass


class CommitChangesWorkflow:
    def __init__(
        self,
        *,
        workspace_manager: workspace_service.WorkspaceManager | None = None,
        git_svc=git_service,
    ) -> None:
        self._manager = workspace_manager or workspace_service.manager
        self._git = git_svc

    def commit(
        self,
        *,
        workspace_id: str,
        message: str,
        author_name: str,
        author_email: str,
        allow_empty: bool = False,
    ) -> CommitResult:
        ws = self._manager.get(workspace_id)
        files = self._git.changed_files(ws.path)
        if not files and not allow_empty:
            raise NoChangesToCommitError("no changes to commit")
        sha = self._git.commit_all(
            repo_path=ws.path,
            message=message,
            author_name=author_name,
            author_email=author_email,
            allow_empty=allow_empty,
        )
        return CommitResult(commit_sha=sha, files_changed=files)


commit_changes_workflow = CommitChangesWorkflow()
