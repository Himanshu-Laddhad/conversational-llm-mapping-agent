"""
groq_agent.py — compatibility shim.

This module was renamed to explain_agent.py because the agent is not
Groq-specific — it supports OpenAI, Groq, NVIDIA NIM, and other providers.

All imports that previously used groq_agent will continue to work unchanged.
"""
from .explain_agent import explain  # noqa: F401 — re-export for backward compat

__all__ = ["explain"]
