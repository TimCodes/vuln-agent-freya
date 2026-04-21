"""GitHub domain service.

Parses owner/repo out of git URLs, builds HTTPS push URLs with an
embedded token, and creates pull requests via the low-level
`github_api_client`. No workspace or subprocess awareness here.
"""
from __future__ import annotations

import re

from ..core.clients import github_api_client as _github_api_client_module
from ..core.clients.github_api_client import GitHubApiError, github_api_client


GITHUB_URL_PATTERNS = [
    re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"),
]


class GithubError(Exception):
    pass


def parse_github_repo(repo_url: str) -> tuple[str, str]:
    """Return (owner, repo) for a GitHub HTTPS or SSH URL."""
    for pattern in GITHUB_URL_PATTERNS:
        m = pattern.match(repo_url.strip())
        if m:
            return m.group(1), m.group(2)
    raise GithubError(f"not a recognized GitHub URL: {repo_url}")


def push_url_for(owner: str, repo: str, token: str) -> str:
    """Build an HTTPS push URL that embeds a token for authentication.
    `x-access-token` is the conventional username for GitHub tokens."""
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def create_pull_request(
    *,
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    token: str,
) -> dict:
    try:
        return github_api_client.create_pull_request(
            owner=owner,
            repo=repo,
            head=head,
            base=base,
            title=title,
            body=body,
            token=token,
        )
    except GitHubApiError as e:
        raise GithubError(str(e)) from e


# Keep the low-level module accessible for tests that want to stub the client.
_client_module = _github_api_client_module
