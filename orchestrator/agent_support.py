"""Shared helpers for reasoning-step spec.py files (framework-neutral).

spec.py files declare tool NEEDS; the adapter materializes them. This
module therefore imports no agent framework.
"""

import os

from orchestrator.invoker import StoreTools


def gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


def store_toolset(tool_filter: list[str]) -> StoreTools:
    """Declare a narrow, role-scoped slice of the delivery store."""
    return StoreTools(tuple(tool_filter))
