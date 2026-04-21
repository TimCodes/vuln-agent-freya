"""Low-level npm subprocess client."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class NpmClientError(Exception):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def _resolve_npm() -> str:
    # On Windows `npm` is a .cmd shim; subprocess with shell=False won't find
    # it from a bare "npm" arg. shutil.which consults PATHEXT and returns the
    # real path (e.g. npm.cmd), which subprocess can execute directly.
    resolved = shutil.which("npm")
    if not resolved:
        raise NpmClientError("npm executable not found in PATH")
    return resolved


def run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    if args and args[0] == "npm":
        args = [_resolve_npm(), *args[1:]]
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise NpmClientError(f"npm command timed out after {timeout}s: {' '.join(args)}") from e
    except FileNotFoundError as e:
        raise NpmClientError("npm executable not found in PATH") from e
