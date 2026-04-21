"""Remediation service — the orchestration seam between API and graph.

Owns:
  * invoking the compiled LangGraph workflow,
  * best-effort workspace cleanup,
  * coercing LangGraph's loose dict/model state back into the response DTO.

The router is intentionally dumb: it validates the request model and hands
it to `remediation_service.remediate(body)`.
"""
from __future__ import annotations

from ..clients import ToolsApiClient, ToolsApiError
from ..core import get_logger
from ..graph.builder import compiled_graph
from ..schemas.models import AppliedFix, RemediateRequest, RemediationResult

logger = get_logger("agent.remediation_service")


class RemediationService:
    """Runs the remediation graph and shapes the response."""

    def __init__(self, graph=None) -> None:
        # Injectable so tests can pass a stub compiled graph.
        self._graph = graph or compiled_graph

    @staticmethod
    def _coerce_applied(items) -> list[AppliedFix]:
        out: list[AppliedFix] = []
        for item in items or []:
            if isinstance(item, AppliedFix):
                out.append(item)
            elif isinstance(item, dict):
                try:
                    out.append(AppliedFix.model_validate(item))
                except Exception:
                    continue
        return out

    async def _cleanup_workspace(self, workspace_id: str | None) -> None:
        """Best-effort workspace delete. Failures are logged, not raised —
        in a real deployment this would be a background task or TTL reaper
        on the Tools API side."""
        if not workspace_id:
            return
        try:
            async with ToolsApiClient() as client:
                await client.delete_workspace(workspace_id)
        except ToolsApiError as e:
            logger.warning("workspace cleanup failed for %s: %s", workspace_id, e)

    async def remediate(self, body: RemediateRequest) -> RemediationResult:
        initial_state = {
            "repo_url": body.repo_url,
            "branch": body.branch,
            "reported_vulnerabilities": body.vulnerabilities,
            "errors": [],
            "applied_fixes": [],
            "fix_plan": [],
        }

        logger.info(
            "remediate start repo=%s branch=%s reported=%d",
            body.repo_url,
            body.branch,
            len(body.vulnerabilities),
        )

        final_state = await self._graph.ainvoke(initial_state)

        workspace_id = final_state.get("workspace_id")
        await self._cleanup_workspace(workspace_id)

        return RemediationResult(
            repo_url=body.repo_url,
            branch=body.branch,
            workspace_id=workspace_id,
            applied_fixes=self._coerce_applied(final_state.get("applied_fixes")),
            pr_url=final_state.get("pr_url"),
            summary=final_state.get("summary", ""),
            errors=final_state.get("errors", []),
        )


remediation_service = RemediationService()
