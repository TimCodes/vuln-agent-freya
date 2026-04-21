"""LangGraph wiring.

Graph shape:

    START
      │
      ▼
  create_workspace
      │
      ▼  (skip downstream if no workspace)
  initial_audit ──► plan ──► execute_plan ──► resync_manifest ──► final_audit ──► commit ──► open_pr ──► summarize ──► END
                                                                                                             ▲
  (short-circuit)                                                                                             │
  create_workspace ── workspace_id is None ───────► summarize ────────────────────────────────────────────────┘
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import (
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


def _after_create_workspace(state: AgentState) -> str:
    """If workspace creation failed we skip straight to summarize so the
    caller still gets a report explaining what went wrong."""
    return "initial_audit" if state.get("workspace_id") else "summarize"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("create_workspace", create_workspace_node)
    graph.add_node("initial_audit", initial_audit_node)
    graph.add_node("plan", plan_node)
    graph.add_node("execute_plan", execute_plan_node)
    graph.add_node("resync_manifest", resync_manifest_node)
    graph.add_node("final_audit", final_audit_node)
    graph.add_node("commit", commit_node)
    graph.add_node("open_pr", open_pr_node)
    graph.add_node("summarize", summarize_node)

    graph.set_entry_point("create_workspace")
    graph.add_conditional_edges(
        "create_workspace",
        _after_create_workspace,
        {"initial_audit": "initial_audit", "summarize": "summarize"},
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
