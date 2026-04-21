"""Pull request workflow.

Steps:
  1. Look up the workspace.
  2. Parse owner/repo from the clone URL.
  3. `git push HEAD:refs/heads/<head_branch>` via an HTTPS URL that
     embeds the configured token.
  4. Call GitHub to open the PR.

Token scrubbing happens here so the token never leaks into error
responses emitted by the router.
"""
from __future__ import annotations

from ..core.config import settings
from ..models.pull_request_models import PullRequestResult
from ..services import git_service, github_service, workspace_service


class MissingGitHubTokenError(Exception):
    pass


class PushFailedError(Exception):
    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class PullRequestCreationError(Exception):
    pass


def _scrub(text: str | None, token: str) -> str:
    if not text:
        return ""
    return text.replace(token, "***")


class PullRequestWorkflow:
    def __init__(
        self,
        *,
        workspace_manager: workspace_service.WorkspaceManager | None = None,
        git_svc=git_service,
        github_svc=github_service,
    ) -> None:
        self._manager = workspace_manager or workspace_service.manager
        self._git = git_svc
        self._github = github_svc

    def open(
        self,
        *,
        workspace_id: str,
        base_branch: str,
        head_branch: str,
        title: str,
        body: str,
    ) -> PullRequestResult:
        token = settings.github_token
        print("GitHub token used for PR creation: ", token)
        print(token)
        if not token:
            raise MissingGitHubTokenError(
                "GITHUB_TOKEN not configured on the Tools API (set TOOLS_API_GITHUB_TOKEN)"
            )

        ws = self._manager.get(workspace_id)
        owner, repo = self._github.parse_github_repo(ws.repo_url)
        push_url = self._github.push_url_for(owner, repo, token)

        try:
            self._git.push_branch(ws.path, push_url, head_branch)
        except self._git.GitError as e:
            raise PushFailedError(
                f"git push failed: {e}",
                stderr=_scrub(e.stderr, token),
            ) from e

        try:
            pr = self._github.create_pull_request(
                owner=owner,
                repo=repo,
                head=head_branch,
                base=base_branch,
                title=title,
                body=body,
                token=token,
            )
        except self._github.GithubError as e:
            raise PullRequestCreationError(
                f"GitHub PR create failed: {_scrub(str(e), token)}"
            ) from e

        return PullRequestResult(
            url=pr["html_url"],
            number=pr["number"],
            head_branch=head_branch,
            base_branch=base_branch,
        )


pull_request_workflow = PullRequestWorkflow()
