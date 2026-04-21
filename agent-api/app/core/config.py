"""Configuration for the Agent API.

Values are loaded from the repo-root `.env` file via python-dotenv
(through pydantic-settings). Real secrets must live in `.env`; this
module only holds non-sensitive defaults.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo-root .env: agent-api/app/core/config.py -> vulnfix-poc/.env
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_API_",
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Tools API ---
    tools_api_base_url: str = "http://localhost:8001"
    tools_api_key: str = "pick-something-random"
    tools_api_timeout_seconds: float = 360.0

    # --- LLM ---
    # Model used for analysis/planning. Keep this an env-driven knob; different
    # stages may warrant different models in the future.
    llm_provider: str = "openai"  # "anthropic" | "openai" | "github"
    llm_model: str = "gpt-4.1-nano-2025-04-14"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    # Provider SDKs read their respective keys from env directly; surfaced here
    # so misconfig shows up early. Real values come from .env.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    gh_api_token: str | None = None

    # --- GitHub Models ---
    github_models_endpoint: str = "https://models.github.ai/inference"
    github_models_token: str | None = None  # falls back to gh_api_token / GITHUB_TOKEN env var

    # --- workflow ---
    # Hard cap on how many targeted package updates a single run may apply.
    # Prevents a runaway plan.
    max_targeted_updates_per_run: int = 25

    # Default commit identity used on the Tools API.
    commit_author_name: str = "VulnFix Bot"
    commit_author_email: str = "vulnfix-bot@example.com"

    # Prefix for auto-generated PR branch names. Final form: "<prefix><ws-id8>".
    pr_branch_prefix: str = "vulnfix/"

    # --- batch CSV upload ---
    # Base URL prepended to the repo slug from the CSV `Location` column.
    # Slug form: "<owner>/<repo>" → "<github_host><owner>/<repo>.git".
    github_host: str = "https://github.com/"
    # How many per-repo remediations run concurrently. Extra repos queue.
    batch_remediation_concurrency: int = 3
    # Truncate the CSV Description field before handing it to the planner —
    # keeps the LLM context from ballooning on reports with long advisories.
    csv_description_max_chars: int = 500


settings = Settings()
