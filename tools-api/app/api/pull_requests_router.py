"""Open a pull request for a workspace: push a branch and call GitHub."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.logger import get_logger
from ..models.pull_request_models import PullRequestRequest, PullRequestResult
from ..services import github_service
from ..services.workspace_service import WorkspaceError
from ..workflows import pull_request_workflow
from ..workflows.pull_request_workflow import (
    MissingGitHubTokenError,
    PullRequestCreationError,
    PushFailedError,
)

logger = get_logger("tools_api.pull_requests")

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["pull_requests"])


@router.post("/pull-request", response_model=PullRequestResult)
def open_pull_request(workspace_id: str, body: PullRequestRequest) -> PullRequestResult:
    try:
        return pull_request_workflow.open(
            workspace_id=workspace_id,
            base_branch=body.base_branch,
            head_branch=body.head_branch,
            title=body.title,
            body=body.body,
        )
    except MissingGitHubTokenError as e:
        logger.exception("pull request blocked, missing GitHub token: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=500, detail=str(e))
    except WorkspaceError as e:
        logger.exception("workspace lookup failed for pull request: workspace_id=%s", workspace_id)
        raise HTTPException(status_code=404, detail=str(e))
    except github_service.GithubError as e:
        # Raised during URL parsing (pre-push).
        logger.exception(
            "github service error on pull request: workspace_id=%s head=%s base=%s",
            workspace_id, body.head_branch, body.base_branch,
        )
        raise HTTPException(status_code=400, detail=str(e))
    except PushFailedError as e:
        logger.exception(
            "git push failed for pull request: workspace_id=%s head=%s stderr=%s",
            workspace_id, body.head_branch, e.stderr,
        )
        raise HTTPException(status_code=500, detail=f"{e} :: {e.stderr}")
    except PullRequestCreationError as e:
        logger.exception(
            "pull request creation failed: workspace_id=%s head=%s base=%s",
            workspace_id, body.head_branch, body.base_branch,
        )
        raise HTTPException(status_code=500, detail=str(e))
