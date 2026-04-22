"""Package-level operations: update a specific package, read package.json, commit."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException

from ..core.logger import get_logger
from ..models.package_models import (
    CommitRequest,
    CommitResult,
    PackageUpdateRequest,
    PackageUpdateResult,
    ResyncManifestRequest,
    ResyncManifestResult,
)
from ..services import git_service, npm_service
from ..services.workspace_service import WorkspaceError, manager
from ..workflows import (
    NoChangesToCommitError,
    commit_changes_workflow,
    package_update_workflow,
)

# Same shape as PackageUpdateRequest.package — path-param validation so we
# never pass user-controlled text to `npm view` without shell-safe checks.
_PKG_NAME_RE = re.compile(r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")

logger = get_logger("tools_api.packages")

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["packages"])


@router.get("/package-json")
def get_package_json(workspace_id: str) -> dict:
    try:
        ws = manager.get(workspace_id)
    except WorkspaceError as e:
        logger.exception("workspace lookup failed for package-json: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
    pkg_path = ws.path / "package.json"
    if not pkg_path.is_file():
        logger.error("package.json not found at %s (workspace_id=%s)", pkg_path, workspace_id)
        raise HTTPException(status_code=404, detail="package.json not found in workspace root")
    try:
        return json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.exception("failed to read package.json at %s", pkg_path)
        raise HTTPException(status_code=500, detail=f"failed to read package.json: {e}")


@router.post("/packages/update", response_model=PackageUpdateResult)
def update_package(workspace_id: str, body: PackageUpdateRequest) -> PackageUpdateResult:
    """Install `body.package@body.version` in the workspace.

    Input is validated by the Pydantic model (strict regex on package name and
    version). The command is invoked with shell=False via an argv list, so
    there's no shell expansion of any value.
    """
    try:
        return package_update_workflow.update(
            workspace_id=workspace_id,
            package=body.package,
            version=body.version,
            dev=body.dev,
        )
    except WorkspaceError as e:
        logger.exception(
            "workspace lookup failed for package update: workspace_id=%s pkg=%s@%s",
            workspace_id, body.package, body.version,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except npm_service.NpmError as e:
        logger.exception(
            "npm install failed: workspace_id=%s pkg=%s@%s stderr=%s",
            workspace_id, body.package, body.version, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"npm install failed: {e} :: {e.stderr}")


@router.get("/packages/{package_name:path}/versions")
def list_package_versions(workspace_id: str, package_name: str) -> dict:
    """Return all published versions of `package_name` from the npm registry.

    Used by the agent's fallback path when an install target (e.g. an invented
    or unpublished version) fails with ETARGET — the agent then picks the
    lowest-impact real release from this list and retries.
    """
    if not _PKG_NAME_RE.fullmatch(package_name):
        raise HTTPException(status_code=400, detail=f"invalid npm package name: {package_name}")
    try:
        versions = package_update_workflow.list_versions(
            workspace_id=workspace_id, package=package_name
        )
    except WorkspaceError as e:
        logger.exception(
            "workspace lookup failed for list versions: workspace_id=%s pkg=%s",
            workspace_id, package_name,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except npm_service.NpmError as e:
        logger.exception(
            "npm view failed: workspace_id=%s pkg=%s stderr=%s",
            workspace_id, package_name, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"npm view failed: {e} :: {e.stderr}")
    return {"package": package_name, "versions": versions}


@router.post("/packages/resync-manifest", response_model=ResyncManifestResult)
def resync_manifest(workspace_id: str, body: ResyncManifestRequest) -> ResyncManifestResult:
    """Make remediation visible in package.json: re-save direct deps at their
    installed versions and pin transitive deps via ``overrides``. See
    npm_service.resync_manifest for the full rationale.
    """
    try:
        outcome = package_update_workflow.resync_manifest(
            workspace_id=workspace_id, packages=body.packages,
        )
    except WorkspaceError as e:
        logger.exception(
            "workspace lookup failed for manifest resync: workspace_id=%s", workspace_id,
        )
        raise HTTPException(status_code=404, detail=str(e))
    except npm_service.NpmError as e:
        logger.exception(
            "npm resync failed: workspace_id=%s stderr=%s", workspace_id, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"manifest resync failed: {e} :: {e.stderr}")
    return ResyncManifestResult(
        rewritten=outcome.get("rewritten") or [],
        overrides_added=outcome.get("overrides_added") or [],
    )


@router.post("/commit", response_model=CommitResult)
def commit_changes(workspace_id: str, body: CommitRequest) -> CommitResult:
    try:
        return commit_changes_workflow.commit(
            workspace_id=workspace_id,
            message=body.message,
            author_name=body.author_name,
            author_email=body.author_email,
            allow_empty=body.allow_empty,
        )
    except WorkspaceError as e:
        logger.exception("workspace lookup failed for commit: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
    except NoChangesToCommitError as e:
        logger.warning("commit skipped (no changes): workspace_id=%s (%s)", workspace_id, e)
        raise HTTPException(status_code=400, detail=str(e))
    except git_service.GitError as e:
        logger.exception(
            "git commit failed: workspace_id=%s stderr=%s", workspace_id, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"git commit failed: {e} :: {e.stderr}")
