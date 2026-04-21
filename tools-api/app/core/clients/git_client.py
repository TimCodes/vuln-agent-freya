"""Low-level git subprocess client.

Single responsibility: run a git command via `subprocess.run` with
`shell=False` and a timeout, return the `CompletedProcess` regardless
of exit code. argv construction and return-code interpretation live
in `services/git_service.py`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitClientError(Exception):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def run(args: list[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a git command. Raises GitClientError on timeout or exec failure."""
    try:
        return subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,  # CRITICAL: never let user input hit a shell.
        )
    except subprocess.TimeoutExpired as e:
        raise GitClientError(f"git command timed out after {timeout}s: {' '.join(args)}") from e
    except FileNotFoundError as e:
        raise GitClientError("git executable not found in PATH") from e
