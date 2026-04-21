"""POST /remediate — the single public endpoint of the Agent API.

The router is a thin HTTP seam: validate, delegate to the service, translate
any unexpected exception into a 500. All orchestration lives in the service.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core import get_logger
from ..schemas.models import RemediateRequest, RemediationResult
from ..services import remediation_service

logger = get_logger("agent.remediate")

router = APIRouter(tags=["remediation"])


@router.post("/remediate", response_model=RemediationResult)
async def remediate(body: RemediateRequest) -> RemediationResult:
    try:
        return await remediation_service.remediate(body)
    except Exception as e:
        # A hard failure inside the graph should still give the caller a
        # meaningful error rather than a 500 with a stack trace.
        logger.exception("graph invocation failed")
        raise HTTPException(status_code=500, detail=f"graph failure: {e}")
