"""LLM client factory.

Builds a LangChain chat model (Anthropic, OpenAI, or GitHub Models) based on settings.
Kept separate from the agents so test code can stub out the factory
without pulling the whole graph module.
"""
from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from .config import settings


def build_chat_llm() -> BaseChatModel:
    """Pick Anthropic, OpenAI, or GitHub Models based on `settings.llm_provider`."""
    provider = (settings.llm_provider or "anthropic").lower()
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    if provider == "github":
        from langchain_openai import ChatOpenAI
        token = (
            settings.github_models_token
            or settings.gh_api_token
            or os.environ.get("GITHUB_TOKEN", "")
        )
        # GitHub Models only supports temperature=1 for some models;
        # omitting it still causes ChatOpenAI to send its default (0.7).
        return ChatOpenAI(
            model=settings.llm_model,
            temperature=1,
            openai_api_key=token,
            openai_api_base=settings.github_models_endpoint,
        )
    raise ValueError(f"unsupported llm_provider: {settings.llm_provider!r}")
