"""Audit-fix workflow.

Steps:
  1. Run `npm audit fix` (SemVer-compatible fixes only).
  2. Re-run `npm audit` so the caller gets a clear "after" view.
     A failure in step 2 is non-fatal; `audit_after` becomes None.
"""
from __future__ import annotations

from ..models.npm_models import NpmAuditFixResult, NpmAuditResult
from ..services import npm_service, workspace_service


class AuditFixWorkflow:
    def __init__(
        self,
        *,
        workspace_manager: workspace_service.WorkspaceManager | None = None,
        npm_svc=npm_service,
    ) -> None:
        self._manager = workspace_manager or workspace_service.manager
        self._npm = npm_svc

    def run(self, workspace_id: str) -> NpmAuditFixResult:
        ws = self._manager.get(workspace_id)
        result = self._npm.audit_fix(ws.path)

        audit_after: NpmAuditResult | None = None
        try:
            audit_after = NpmAuditResult.from_raw(self._npm.audit(ws.path))
        except self._npm.NpmError:
            audit_after = None

        return NpmAuditFixResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            audit_after=audit_after,
        )


audit_fix_workflow = AuditFixWorkflow()
