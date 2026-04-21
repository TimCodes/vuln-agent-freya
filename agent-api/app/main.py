"""Agent API entrypoint.

This service is the "brain" of the VulnFix POC. It:
  * Accepts a remediation request describing a repo and a list of
    vulnerabilities.
  * Runs a LangGraph workflow that decides what to do, then asks the Tools
    API to actually do it.

It does NOT execute shell commands, touch the filesystem, or import
subprocess anywhere in its codebase. All mutations are delegated to the
Tools API over HTTP. This separation is the core security property of the
POC and should not be weakened.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .api import batch_router, remediate_router
from .core import configure_logging, settings

configure_logging()

app = FastAPI(
    title="VulnFix Agent API",
    description="LangGraph-based vulnerability remediation agent. Delegates all mutations to the Tools API.",
    version="0.1.0",
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail if isinstance(exc.detail, str) else "error", "detail": exc.detail},
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "tools_api_base_url": settings.tools_api_base_url,
        "llm_model": settings.llm_model,
    }


app.include_router(remediate_router.router)
app.include_router(batch_router.router)
