"""LangGraph wiring.

Graph shape:

    START
      │
      ▼
    classify
      │
      ├─ nothing to report ─────────────────────────────────► summarize
      │
      ▼
    create_workspace
      │
      ├─ workspace creation failed ────────────────────────► summarize
      │
      ├─ no code vulns, only image / unclassified ────────► commit (empty)
      │
      ▼
    initial_audit ──► plan ──► execute_plan ──► resync_manifest ──► final_audit ──► commit
      │
      ▼
    open_pr ──► summarize

`commit` handles two cases: a real tree diff when code fixes landed, and an
empty commit (`--allow-empty`) when the PR is informational (image /
unclassified rows only). `open_pr` runs whenever a commit exists.
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from ..schemas.models import VulnerabilityReport
from .nodes import (
    classify_node,
    commit_node,
    create_workspace_node,
    execute_plan_node,
    final_audit_node,
    initial_audit_node,
    open_pr_node,
    plan_node,
    resync_manifest_node,
    summarize_node,
)
from .state import AgentState


def _has_any_classified(reports: list[VulnerabilityReport]) -> bool:
    return any(r.kind in ("code", "image", "unclassified") for r in reports)


def _has_code_vulns(reports: list[VulnerabilityReport]) -> bool:
    return any(r.kind == "code" for r in reports)


def _after_classify(state: AgentState) -> str:
    """Skip workspace provisioning entirely when there's nothing the
    remediation pipeline can act on — no code to fix, no image/unclassified
    rows to surface. Saves a clone."""
    reports = state.get("reported_vulnerabilities", []) or []
    return "create_workspace" if _has_any_classified(reports) else "summarize"


def _after_create_workspace(state: AgentState) -> str:
    """Three routes after the clone:

    * Clone failed → skip to summarize; caller still gets a structured error.
    * Clone succeeded but there are no code vulns → skip the audit/plan/
      execute chain and jump to commit (which will write an empty commit so
      the informational PR has a target).
    * Clone succeeded with code vulns → enter the full remediation chain.
    """
    if not state.get("workspace_id"):
        return "summarize"
    reports = state.get("reported_vulnerabilities", []) or []
    return "initial_audit" if _has_code_vulns(reports) else "commit"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("classify", classify_node)
    graph.add_node("create_workspace", create_workspace_node)
    graph.add_node("initial_audit", initial_audit_node)
    graph.add_node("plan", plan_node)
    graph.add_node("execute_plan", execute_plan_node)
    graph.add_node("resync_manifest", resync_manifest_node)
    graph.add_node("final_audit", final_audit_node)
    graph.add_node("commit", commit_node)
    graph.add_node("open_pr", open_pr_node)
    graph.add_node("summarize", summarize_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify",
        _after_classify,
        {"create_workspace": "create_workspace", "summarize": "summarize"},
    )
    graph.add_conditional_edges(
        "create_workspace",
        _after_create_workspace,
        {
            "initial_audit": "initial_audit",
            "commit": "commit",
            "summarize": "summarize",
        },
    )
    graph.add_edge("initial_audit", "plan")
    graph.add_edge("plan", "execute_plan")
    graph.add_edge("execute_plan", "resync_manifest")
    graph.add_edge("resync_manifest", "final_audit")
    graph.add_edge("final_audit", "commit")
    graph.add_edge("commit", "open_pr")
    graph.add_edge("open_pr", "summarize")
    graph.add_edge("summarize", END)

    return graph.compile()


# Compile once at import time; LangGraph graphs are stateless between invocations.
compiled_graph = build_graph()
