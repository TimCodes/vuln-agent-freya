"""npm audit and audit-fix endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.logger import get_logger
from ..models.npm_models import NpmAuditFixResult, NpmAuditResult
from ..services import npm_service
from ..services.workspace_service import WorkspaceError, manager
from ..workflows import audit_fix_workflow

logger = get_logger("tools_api.npm")

router = APIRouter(prefix="/workspaces/{workspace_id}/npm", tags=["npm"])


@router.post("/audit", response_model=NpmAuditResult)
def run_audit(workspace_id: str) -> NpmAuditResult:
    try:
        ws = manager.get(workspace_id)
    except WorkspaceError as e:
        logger.exception("workspace lookup failed for audit: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
    try:
        raw = npm_service.audit(ws.path)
    except npm_service.NpmError as e:
        logger.exception(
            "npm audit failed: workspace_id=%s path=%s stderr=%s",
            workspace_id, ws.path, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"npm audit failed: {e} :: {e.stderr}")
    return NpmAuditResult.from_raw(raw)


@router.post("/audit-fix", response_model=NpmAuditFixResult)
def run_audit_fix(workspace_id: str) -> NpmAuditFixResult:
    try:
        return audit_fix_workflow.run(workspace_id)
    except WorkspaceError as e:
        logger.exception("workspace lookup failed for audit-fix: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
    except npm_service.NpmError as e:
        logger.exception(
            "npm audit fix failed: workspace_id=%s stderr=%s",
            workspace_id, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"npm audit fix failed: {e} :: {e.stderr}")
