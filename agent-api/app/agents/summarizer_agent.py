"""Summarizer agent — writes the human-readable remediation report.

The summarizer takes the accumulated workflow state (before/after audit
summaries, plan, applied fixes, commit/PR info, errors) and produces the
`summary` string returned in the final `RemediationResult`. It is the
user-facing voice of the POC — the planner decides, the executor acts, and
the summarizer explains.

If the LLM call fails the agent returns a deterministic plaintext summary
so the caller always gets *some* report.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ..core import build_chat_llm, get_logger

logger = get_logger("agent.summarizer")


SUMMARIZER_SYSTEM = """You write the one-line status for an automated npm vulnerability remediation run.

Given a JSON record of the run, output ONE or TWO short sentences — at most
~40 words total. State: vulns resolved (count), vulns remaining (count), and
whether a PR was opened. If anything failed, name the package(s). Nothing
else. No preamble, no markdown, no bullets, no code fences. Do not invent
facts that aren't in the input.
"""


class SummarizerAgent:
    """Turns the final workflow state into a human-readable report.

    Responsibilities
    ----------------
    * Call the LLM with a structured JSON payload describing the run.
    * Fall back to a deterministic plaintext summary if the LLM is
      unavailable or raises.
    * Never mutate state, never invent facts not in the input payload.

    The agent is invoked once, at the tail end of the graph, and its output
    is copied verbatim into the API response.
    """

    def __init__(self) -> None:
        self._llm: BaseChatModel | None = None

    def _get_llm(self) -> BaseChatModel:
        """Lazy-build the LLM client on first use."""
        if self._llm is None:
            self._llm = build_chat_llm()
        return self._llm

    def heuristic_summary(self, payload: dict[str, Any]) -> str:
        """One-line fallback used when the LLM is unavailable."""
        before = payload.get("initial_audit_summary", {}).get("total", 0)
        after = payload.get("final_audit_summary", {}).get("total", 0)
        applied = payload.get("applied_fixes", []) or []
        failed_pkgs = [
            (f.get("action") or {}).get("package")
            for f in applied
            if not f.get("success") and (f.get("action") or {}).get("package")
        ]
        pr = (payload.get("pull_request") or {}).get("url")
        parts = [f"Resolved {max(before - after, 0)} of {before} vuln(s); {after} remaining."]
        if failed_pkgs:
            parts.append(f"Failed: {', '.join(failed_pkgs)}.")
        parts.append("PR opened." if pr else "No PR opened.")
        return " ".join(parts)

    async def summarize(self, payload: dict[str, Any]) -> str:
        """Call the LLM to produce a human-readable summary; fall back to
        a deterministic plaintext report if the call fails."""
        try:
            resp = await self._get_llm().ainvoke([
                SystemMessage(content=SUMMARIZER_SYSTEM),
                HumanMessage(content=json.dumps(payload, indent=2, default=str)),
            ])
            return resp.content if isinstance(resp.content, str) else str(resp.content)
        except Exception as e:
            logger.warning("summarizer LLM call failed: %s", e)
            return self.heuristic_summary(payload)


summarizer_agent = SummarizerAgent()
