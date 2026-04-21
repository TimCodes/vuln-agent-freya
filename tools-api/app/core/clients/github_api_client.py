"""Low-level GitHub REST API client.

Thin `httpx` wrapper around api.github.com. Knows nothing about
workspaces, git URLs, or pull-request orchestration.
"""
from __future__ import annotations

import httpx


class GitHubApiError(Exception):
    pass


class GitHubApiClient:
    BASE_URL = "https://api.github.com"

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def create_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        token: str,
    ) -> dict:
        resp = httpx.post(
            f"{self.BASE_URL}/repos/{owner}/{repo}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "head": head, "base": base, "body": body},
            timeout=self._timeout,
        )
        if resp.status_code >= 400:
            raise GitHubApiError(
                f"GitHub API returned {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()


github_api_client = GitHubApiClient()
