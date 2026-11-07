"""A Strands hook that logs the agent's inner steps (tool calls, model calls) for DEBUG visibility."""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookRegistry,
    MessageAddedEvent,
)

from ..core.logging import log

logger = logging.getLogger("kiro_crew.agents.trace")


def _result_summary(result: Any) -> str:
    if isinstance(result, Exception):
        return f"error: {type(result).__name__}: {result}"
    try:
        status = result.get("status")
        blocks = result.get("content", [])
        text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and "text" in b)
        return f"{status}: {text}" if text else str(status)
    except Exception:  # noqa: BLE001
        return str(result)[:200]


class TraceHook:
    """Logs tool and model activity at DEBUG. Cheap when DEBUG is off (guarded by ``log``)."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool)
        registry.add_callback(AfterToolCallEvent, self._after_tool)
        registry.add_callback(BeforeModelCallEvent, self._before_model)
        registry.add_callback(AfterModelCallEvent, self._after_model)
        registry.add_callback(MessageAddedEvent, self._message)

    def _before_tool(self, event: BeforeToolCallEvent) -> None:
        tu = event.tool_use
        log(logger, logging.DEBUG, "tool call", tool=tu.get("name"), args=tu.get("input"))

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        tu = event.tool_use
        ms = int((event.duration or 0) * 1000)
        log(logger, logging.DEBUG, "tool result", tool=tu.get("name"), ms=ms,
            result=_result_summary(event.result))

    def _before_model(self, event: BeforeModelCallEvent) -> None:
        log(logger, logging.DEBUG, "model call")

    def _after_model(self, event: AfterModelCallEvent) -> None:
        log(logger, logging.DEBUG, "model returned")

    def _message(self, event: MessageAddedEvent) -> None:
        msg = event.message
        kinds = [k for b in msg.get("content", []) for k in b] if isinstance(msg.get("content"), list) else []
        log(logger, logging.DEBUG, "message added", role=msg.get("role"), blocks=kinds)
