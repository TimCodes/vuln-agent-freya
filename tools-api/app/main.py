"""Tools API entrypoint.

The Tools API is the ONLY component allowed to perform mutations on a
checked-out repository (git, npm, filesystem). It exposes a small, explicit
set of endpoints and is meant to run in an isolated container with network
access restricted to what it needs (git remote, npm registry).

It does NOT talk to any LLM. It has no awareness of "vulnerabilities" or
"agents"; it's just a set of typed, validated wrappers around git and npm.

Layout follows the FWAE layered pattern:
    api       -> HTTP routing and response shaping
    workflows -> multi-step use-case orchestration
    services  -> domain rules over low-level clients
    core      -> configuration, logging, transport clients
    models    -> request/response DTOs
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .api import npm_router, packages_router, pull_requests_router, workspaces_router
from .core.config import settings
from .core.logger import configure_logging, get_logger

configure_logging()
logger = get_logger("tools_api")

app = FastAPI(
    title="VulnFix Tools API",
    description="Performs git/npm mutations on behalf of the VulnFix agent.",
    version="0.1.0",
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Uniform error shape.
    logger.error(
        "HTTPException %s on %s %s: %s",
        exc.status_code, request.method, request.url.path, exc.detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail if isinstance(exc.detail, str) else "error",
            "detail": exc.detail,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Surface full traceback + exception details for anything not caught upstream.
    logger.exception(
        "Unhandled exception on %s %s: %r",
        request.method, request.url.path, exc,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "internal server error", "detail": repr(exc)},
    )


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "workspace_root": str(settings.workspace_root)}


app.include_router(workspaces_router.router)
app.include_router(npm_router.router)
app.include_router(packages_router.router)
app.include_router(pull_requests_router.router)
