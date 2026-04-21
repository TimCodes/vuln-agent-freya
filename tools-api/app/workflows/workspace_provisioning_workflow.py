"""Workspace provisioning workflow.

Steps:
  1. Reserve a UUID-keyed directory under workspace_root.
  2. Clone the requested repo/branch into it.
  3. Read HEAD commit for the workspace record.
  4. On clone failure, roll back the reservation.
"""
from __future__ import annotations

import logging

from ..services import git_service, npm_service, workspace_service
from ..services.workspace_service import Workspace, WorkspaceError

logger = logging.getLogger(__name__)


class WorkspaceProvisioningWorkflow:
    def __init__(
        self,
        *,
        workspace_manager: workspace_service.WorkspaceManager | None = None,
        git_svc=git_service,
        npm_svc=npm_service,
    ) -> None:
        self._manager = workspace_manager or workspace_service.manager
        self._git = git_svc
        self._npm = npm_svc

    def create(
        self, *, repo_url: str, branch: str, depth: int | None
    ) -> tuple[Workspace, str | None]:
        ws = self._manager.reserve(repo_url=repo_url, branch=branch)
        try:
            self._git.clone(repo_url=repo_url, branch=branch, dest=ws.path, depth=depth)
            commit = self._git.current_commit(ws.path)
        except self._git.GitError:
            # Roll back the reservation if the clone fails.
            try:
                self._manager.delete(ws.workspace_id)
            except WorkspaceError:
                pass
            raise

        # npm audit on a freshly cloned repo (no node_modules) returns
        # incomplete results, so install deps before any audit runs.
        # Non-Node repos (no package.json) skip this step.
        if (ws.path / "package.json").exists():
            try:
                result = self._npm.install_all(ws.path)
                if result.returncode != 0:
                    logger.error(
                        "npm install after clone exited %d for workspace %s\n"
                        "stdout:\n%s\nstderr:\n%s",
                        result.returncode,
                        ws.workspace_id,
                        (result.stdout or "")[:2000],
                        (result.stderr or "")[:2000],
                    )
                else:
                    logger.info(
                        "npm install after clone succeeded for workspace %s",
                        ws.workspace_id,
                    )
            except self._npm.NpmError as e:
                logger.exception(
                    "npm install after clone raised for workspace %s: %s :: stderr=%s",
                    ws.workspace_id, e, getattr(e, "stderr", ""),
                )

        return ws, commit


workspace_provisioning_workflow = WorkspaceProvisioningWorkflow()
