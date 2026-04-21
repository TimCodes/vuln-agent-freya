"""Configuration for the Tools API.

Values are loaded from the repo-root `.env` file via python-dotenv
(through pydantic-settings). All paths must be absolute.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env: tools-api/app/core/config.py -> vulnfix-poc/.env
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOOLS_API_",
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base directory where all workspaces are checked out. Every workspace
    # is placed in a subdirectory named by its UUID. Path traversal outside
    # of this directory is rejected by the workspace manager.
    workspace_root: Path = Path("/tmp/vulnfix-workspaces")

    # Subprocess hard timeouts (seconds). npm install can be slow; keep
    # these generous but bounded so a hung process can't pin the service.
    git_timeout_seconds: int = 120
    npm_audit_timeout_seconds: int = 120
    npm_install_timeout_seconds: int = 300

    # Max number of concurrent workspaces. Prevents unbounded disk usage.
    max_workspaces: int = 50

    # Token used to push branches and open pull requests on GitHub. Required
    # for the /pull-request endpoint; if unset, that endpoint returns 500.
    github_token: str | None = None


settings = Settings()
