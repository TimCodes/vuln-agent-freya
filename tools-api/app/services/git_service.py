"""Git domain service.

Thin domain layer over `core/clients/git_client.py`. Knows git command
semantics (clone flags, how to read `--porcelain` output, how to create
a commit without mutating repo config). The underlying subprocess
mechanics live in the client.
"""
from __future__ import annotations

from pathlib import Path

from ..core.clients import git_client
from ..core.config import settings


class GitError(Exception):
    def __init__(self, message: str, stderr: str = "", exit_code: int | None = None) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


def _run(args: list[str], cwd: Path | None, timeout: int):
    try:
        return git_client.run(args, cwd=cwd, timeout=timeout)
    except git_client.GitClientError as e:
        raise GitError(str(e), stderr=e.stderr) from e


def clone(repo_url: str, branch: str, dest: Path, depth: int | None = 1) -> None:
    """Clone repo_url at branch into dest."""
    args = ["git", "clone", "--branch", branch, "--single-branch"]
    if depth is not None and depth > 0:
        args += ["--depth", str(depth)]
    args += ["--", repo_url, str(dest)]
    result = _run(args, cwd=None, timeout=settings.git_timeout_seconds)
    if result.returncode != 0:
        raise GitError(
            f"git clone failed (exit {result.returncode})",
            stderr=result.stderr,
            exit_code=result.returncode,
        )


def current_commit(repo_path: Path) -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=repo_path, timeout=settings.git_timeout_seconds)
    if result.returncode != 0:
        raise GitError("git rev-parse failed", stderr=result.stderr, exit_code=result.returncode)
    return result.stdout.strip()


def changed_files(repo_path: Path) -> list[str]:
    """Return the list of files modified since HEAD (unstaged + staged)."""
    result = _run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
        timeout=settings.git_timeout_seconds,
    )
    if result.returncode != 0:
        raise GitError("git status failed", stderr=result.stderr, exit_code=result.returncode)
    files: list[str] = []
    for line in result.stdout.splitlines():
        # line is like ' M path/to/file' or '?? path/to/file'
        if len(line) > 3:
            files.append(line[3:].strip())
    return files


def push_branch(repo_path: Path, push_url: str, branch: str) -> None:
    """Push the current HEAD to `branch` on the remote at `push_url`.
    Does not modify any existing remote named `origin`."""
    args = ["git", "push", push_url, f"HEAD:refs/heads/{branch}"]
    result = _run(args, cwd=repo_path, timeout=settings.git_timeout_seconds)
    if result.returncode != 0:
        raise GitError(
            f"git push failed (exit {result.returncode})",
            stderr=result.stderr,
            exit_code=result.returncode,
        )


def commit_all(repo_path: Path, message: str, author_name: str, author_email: str) -> str:
    """Stage all changes and create a commit. Returns commit SHA."""
    r1 = _run(["git", "add", "-A"], cwd=repo_path, timeout=settings.git_timeout_seconds)
    if r1.returncode != 0:
        raise GitError("git add failed", stderr=r1.stderr, exit_code=r1.returncode)

    # -c flags set author/committer identity without mutating repo config.
    commit_args = [
        "git",
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
        "commit", "-m", message,
    ]
    r2 = _run(commit_args, cwd=repo_path, timeout=settings.git_timeout_seconds)
    if r2.returncode != 0:
        raise GitError(
            f"git commit failed (exit {r2.returncode})",
            stderr=r2.stderr or r2.stdout,
            exit_code=r2.returncode,
        )

    return current_commit(repo_path)
