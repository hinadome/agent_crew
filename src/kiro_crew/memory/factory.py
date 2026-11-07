"""Single place that builds a long-term memory store for an owner (used by agents, chat commands, CLI)."""

from __future__ import annotations

from typing import Any

from ..core.context import AppContext
from . import hybrid, inmem  # noqa: F401  (register the 'hybrid' and 'inmem' backends)
from .longterm import memory_backends


def namespace_for(ctx: AppContext, owner: str, agent: str | None = None) -> str:
    """``user:<owner>`` normally; ``agent:<name>:user:<owner>`` for agents with private memory, so an isolated
    agent can neither read nor write what the owner's other agents know."""
    if agent is not None and agent in ctx.config.agents and ctx.config.agents[agent].private_memory:
        return f"agent:{agent}:user:{owner}"
    return f"user:{owner}"


def make_store(ctx: AppContext, owner: str, *, extraction: Any = False, agent: str | None = None) -> Any:
    m = ctx.config.memory
    return memory_backends.get(m.backend)(ctx.storage, namespace_for(ctx, owner, agent), extraction=extraction,
                                          **dict(m.options))
