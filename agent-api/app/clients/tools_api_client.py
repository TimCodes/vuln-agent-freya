"""HTTP client for the Tools API.

This is the ONLY path through which the agent causes any mutation. Everything
that would change a file, run git, or run npm goes through here. The agent
itself does not import subprocess and does not touch the filesystem.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..core import get_logger
from ..core.config import settings

logger = get_logger("agent.tools_client")


class ToolsApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ToolsApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._base_url = (base_url or settings.tools_api_base_url).rstrip("/")
        self._headers = {"X-Tools-API-Key": api_key or settings.tools_api_key}
        self._timeout = timeout or settings.tools_api_timeout_seconds

    async def __aenter__(self) -> "ToolsApiClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            logger.exception("HTTP error calling Tools API %s %s", method, path)
            raise ToolsApiError(f"HTTP error calling Tools API {method} {path}: {e}") from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            logger.error(
                "Tools API %s %s returned %s: %r",
                method, path, resp.status_code, body,
            )
            raise ToolsApiError(
                f"Tools API returned {resp.status_code} for {method} {path}",
                status_code=resp.status_code,
                body=body,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- workspaces ---

    async def create_workspace(self, repo_url: str, branch: str, depth: int | None = 1) -> dict:
        return await self._request(
            "POST",
            "/workspaces",
            json={"repo_url": repo_url, "branch": branch, "depth": depth},
        )

    async def delete_workspace(self, workspace_id: str) -> None:
        await self._request("DELETE", f"/workspaces/{workspace_id}")

    # --- npm ---

    async def npm_audit(self, workspace_id: str) -> dict:
        return await self._request("POST", f"/workspaces/{workspace_id}/npm/audit")

    async def npm_audit_fix(self, workspace_id: str) -> dict:
        return await self._request("POST", f"/workspaces/{workspace_id}/npm/audit-fix")

    # --- packages ---

    async def get_package_json(self, workspace_id: str) -> dict:
        return await self._request("GET", f"/workspaces/{workspace_id}/package-json")

    async def update_package(
        self, workspace_id: str, package: str, version: str, dev: bool = False
    ) -> dict:
        return await self._request(
            "POST",
            f"/workspaces/{workspace_id}/packages/update",
            json={"package": package, "version": version, "dev": dev},
        )

    async def list_package_versions(self, workspace_id: str, package: str) -> list[str]:
        resp = await self._request(
            "GET",
            f"/workspaces/{workspace_id}/packages/{package}/versions",
        )
        versions = (resp or {}).get("versions") or []
        return [v for v in versions if isinstance(v, str)]

    async def resync_manifest(
        self, workspace_id: str, packages: list[str] | None = None,
    ) -> dict[str, list[str]]:
        resp = await self._request(
            "POST",
            f"/workspaces/{workspace_id}/packages/resync-manifest",
            json={"packages": packages},
        )
        rewritten = [p for p in (resp or {}).get("rewritten") or [] if isinstance(p, str)]
        overrides_added = [
            p for p in (resp or {}).get("overrides_added") or [] if isinstance(p, str)
        ]
        return {"rewritten": rewritten, "overrides_added": overrides_added}

    async def commit(
        self, workspace_id: str, message: str, author_name: str, author_email: str
    ) -> dict:
        return await self._request(
            "POST",
            f"/workspaces/{workspace_id}/commit",
            json={
                "message": message,
                "author_name": author_name,
                "author_email": author_email,
            },
        )

    # --- pull requests ---

    async def open_pull_request(
        self,
        workspace_id: str,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str = "",
    ) -> dict:
        return await self._request(
            "POST",
            f"/workspaces/{workspace_id}/pull-request",
            json={
                "base_branch": base_branch,
                "head_branch": head_branch,
                "title": title,
                "body": body,
            },
        )
