"""Package update workflow.

Steps:
  1. `npm install <package>@<version>` (with --save or --save-dev).
  2. On success, read the installed version via `npm ls` so the caller
     can confirm what was actually resolved.
"""
from __future__ import annotations

from ..models.package_models import PackageUpdateResult
from ..services import npm_service, workspace_service


class PackageUpdateWorkflow:
    def __init__(
        self,
        *,
        workspace_manager: workspace_service.WorkspaceManager | None = None,
        npm_svc=npm_service,
    ) -> None:
        self._manager = workspace_manager or workspace_service.manager
        self._npm = npm_svc

    def update(
        self, *, workspace_id: str, package: str, version: str, dev: bool
    ) -> PackageUpdateResult:
        ws = self._manager.get(workspace_id)
        result = self._npm.install_package(
            repo_path=ws.path, package=package, version=version, dev=dev
        )

        installed: str | None = None
        if result.returncode == 0:
            try:
                installed = self._npm.installed_version(ws.path, package)
            except self._npm.NpmError:
                installed = None

        return PackageUpdateResult(
            package=package,
            version=version,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            installed_version=installed,
        )

    def list_versions(self, *, workspace_id: str, package: str) -> list[str]:
        ws = self._manager.get(workspace_id)
        return self._npm.list_versions(repo_path=ws.path, package=package)

    def resync_manifest(
        self, *, workspace_id: str, packages: list[str] | None = None,
    ) -> dict[str, list[str]]:
        ws = self._manager.get(workspace_id)
        return self._npm.resync_manifest(repo_path=ws.path, packages=packages)


package_update_workflow = PackageUpdateWorkflow()
