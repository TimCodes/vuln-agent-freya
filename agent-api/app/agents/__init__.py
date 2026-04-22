"""Agent classes that own LLM-backed reasoning.

Each agent is a module-level singleton. Importing this package has no side
effects beyond constructing the singletons; LLM clients are built lazily on
first call.
"""
from .classifier_agent import ClassifierAgent, classifier_agent
from .planner_agent import PlannerAgent, planner_agent
from .summarizer_agent import SummarizerAgent, summarizer_agent

__all__ = [
    "ClassifierAgent",
    "classifier_agent",
    "PlannerAgent",
    "planner_agent",
    "SummarizerAgent",
    "summarizer_agent",
]
