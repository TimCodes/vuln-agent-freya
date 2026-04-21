"""Workspace service.

Responsible for:
  * Creating isolated directories for each workspace (one per UUID)
  * Preventing path traversal (every resolved path must live under workspace_root)
  * Tracking workspace metadata in memory
  * Cleaning up workspaces on delete

This is intentionally in-memory only for the POC. In production this would
be backed by persistent storage (database + durable volume).
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import settings


def _force_writable_and_retry(func, path, _exc):
    """rmtree error handler for Windows: git marks pack/.idx files read-only,
    which blocks os.unlink. Clear the read-only bit and retry."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _rmtree(path: Path) -> None:
    # shutil.rmtree renamed `onerror` → `onexc` in 3.12; support both.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=_force_writable_and_retry)


@dataclass
class Workspace:
    workspace_id: str
    repo_url: str
    branch: str
    path: Path
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WorkspaceError(Exception):
    """Raised for any workspace lifecycle or safety violation."""


class WorkspaceManager:
    """Thread-safe registry of workspaces keyed by UUID."""

    def __init__(self, root: Path, max_workspaces: int) -> None:
        self._root = root.resolve()
        self._max = max_workspaces
        self._workspaces: dict[str, Workspace] = {}
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def reserve(self, repo_url: str, branch: str) -> Workspace:
        """Allocate a workspace directory. The directory is created empty;
        the caller is responsible for populating it (e.g. via git clone)."""
        with self._lock:
            if len(self._workspaces) >= self._max:
                raise WorkspaceError(
                    f"workspace limit reached ({self._max}); delete unused workspaces first"
                )
            ws_id = str(uuid.uuid4())
            path = (self._root / ws_id).resolve()
            self._assert_under_root(path)
            path.mkdir(parents=True, exist_ok=False)
            ws = Workspace(workspace_id=ws_id, repo_url=repo_url, branch=branch, path=path)
            self._workspaces[ws_id] = ws
            return ws

    def get(self, workspace_id: str) -> Workspace:
        ws = self._workspaces.get(workspace_id)
        if ws is None:
            raise WorkspaceError(f"workspace not found: {workspace_id}")
        self._assert_under_root(ws.path)
        if not ws.path.is_dir():
            raise WorkspaceError(f"workspace directory missing on disk: {workspace_id}")
        return ws

    def list(self) -> list[Workspace]:
        return list(self._workspaces.values())

    def delete(self, workspace_id: str) -> None:
        with self._lock:
            ws = self._workspaces.pop(workspace_id, None)
            if ws is None:
                raise WorkspaceError(f"workspace not found: {workspace_id}")
            self._assert_under_root(ws.path)
            if ws.path.exists():
                _rmtree(ws.path)

    def _assert_under_root(self, path: Path) -> None:
        """Guarantee `path` resolves to somewhere under workspace_root.
        Rejects symlink escapes and .. traversal."""
        resolved = path.resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as e:
            raise WorkspaceError(
                f"path escape detected: {resolved} is not under {self._root}"
            ) from e


manager = WorkspaceManager(root=settings.workspace_root, max_workspaces=settings.max_workspaces)
