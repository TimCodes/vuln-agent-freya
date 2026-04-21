"""POST /remediate/batch — CSV upload → fan-out remediation across repos.

Accepts multipart/form-data with a single `file` field (the vulnerability
CSV). Returns a `BatchRemediationResult` with one entry per repo.
"""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..core import get_logger
from ..schemas.models import BatchRemediationResult
from ..services import batch_remediation_service

logger = get_logger("agent.batch")

router = APIRouter(tags=["remediation"])


@router.post("/remediate/batch", response_model=BatchRemediationResult)
async def remediate_batch(file: UploadFile = File(...)) -> BatchRemediationResult:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="upload must be a .csv file")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    try:
        return await batch_remediation_service.remediate_batch(contents)
    except Exception as e:
        logger.exception("batch remediation failed")
        raise HTTPException(status_code=500, detail=f"batch failure: {e}")
