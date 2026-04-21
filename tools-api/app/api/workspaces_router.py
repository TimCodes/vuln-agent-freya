"""Workspaces: create, get, delete."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..core.logger import get_logger
from ..models.workspace_models import CreateWorkspaceRequest, WorkspaceInfo
from ..services import git_service
from ..services.workspace_service import Workspace, WorkspaceError, manager
from ..workflows import workspace_provisioning_workflow

logger = get_logger("tools_api.workspaces")

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _to_info(ws: Workspace, commit: str | None = None) -> WorkspaceInfo:
    return WorkspaceInfo(
        workspace_id=ws.workspace_id,
        repo_url=ws.repo_url,
        branch=ws.branch,
        path=str(ws.path),
        created_at=ws.created_at,
        current_commit=commit,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=WorkspaceInfo)
def create_workspace(body: CreateWorkspaceRequest) -> WorkspaceInfo:
    """Create a new workspace by cloning `repo_url` at `branch`."""
    try:
        ws, commit = workspace_provisioning_workflow.create(
            repo_url=body.repo_url, branch=body.branch, depth=body.depth
        )
    except WorkspaceError as e:
        logger.exception(
            "workspace reservation failed: repo_url=%s branch=%s", body.repo_url, body.branch,
        )
        raise HTTPException(status_code=409, detail=str(e))
    except git_service.GitError as e:
        logger.exception(
            "git clone failed: repo_url=%s branch=%s stderr=%s",
            body.repo_url, body.branch, e.stderr,
        )
        raise HTTPException(status_code=400, detail=f"git clone failed: {e} :: {e.stderr}")

    return _to_info(ws, commit=commit)


@router.get("/{workspace_id}", response_model=WorkspaceInfo)
def get_workspace(workspace_id: str) -> WorkspaceInfo:
    try:
        ws = manager.get(workspace_id)
    except WorkspaceError as e:
        logger.exception("workspace lookup failed: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
    try:
        commit = git_service.current_commit(ws.path)
    except git_service.GitError as e:
        logger.warning(
            "git rev-parse failed for workspace_id=%s: %s :: stderr=%s",
            workspace_id, e, e.stderr,
        )
        commit = None
    return _to_info(ws, commit=commit)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(workspace_id: str) -> None:
    try:
        manager.delete(workspace_id)
    except WorkspaceError as e:
        logger.exception("workspace delete failed: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
