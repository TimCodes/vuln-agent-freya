"""Agent classes that own LLM-backed reasoning.

Each agent is a module-level singleton. Importing this package has no side
effects beyond constructing the singletons; LLM clients are built lazily on
first call.
"""
from .planner_agent import PlannerAgent, planner_agent
from .summarizer_agent import SummarizerAgent, summarizer_agent

__all__ = [
    "PlannerAgent",
    "planner_agent",
    "SummarizerAgent",
    "summarizer_agent",
]
