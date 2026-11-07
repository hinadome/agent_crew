"""Guard rails for autonomous runs: a tool-call budget and repeated-call (stuck loop) detection.

Both first *steer* the model (the tool call is cancelled and the model is told why, so it can change course or
wrap up), and only stop the run hard (``BudgetExceeded``) if it keeps ignoring the message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks import BeforeInvocationEvent, BeforeToolCallEvent, HookRegistry

from ..core.logging import log

logger = logging.getLogger("kiro_crew.agents.budget")


class BudgetExceeded(RuntimeError):
    """The agent ignored repeated warnings and was stopped."""


def find_budget_error(exc: BaseException | None) -> BudgetExceeded | None:
    """Strands wraps hook errors (EventLoopException); dig the BudgetExceeded out of the chain."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, BudgetExceeded):
            return exc
        seen.add(id(exc))
        exc = getattr(exc, "original_exception", None) or exc.__cause__
    return None


class BudgetHook:
    def __init__(self, max_tool_calls: int, repeat_limit: int, grace_calls: int = 3, *,
                 per_invocation: bool = True) -> None:
        self.max_tool_calls = max_tool_calls
        self.repeat_limit = repeat_limit
        self.grace_calls = grace_calls
        self.per_invocation = per_invocation  # False: one budget for the object's whole life (task runs)
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.blocked = 0
        self._last: tuple[str, str] | None = None
        self._streak = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._begin)
        registry.add_callback(BeforeToolCallEvent, self._check)

    def _begin(self, event: BeforeInvocationEvent) -> None:
        if self.per_invocation:
            self.reset()

    def _block(self, event: BeforeToolCallEvent, why: str, advice: str) -> None:
        self.blocked += 1
        name = event.tool_use.get("name")
        log(logger, logging.WARNING, "tool call blocked", tool=name, reason=why, blocked=self.blocked)
        if self.blocked > self.grace_calls:
            raise BudgetExceeded(f"stopped: {why} (the agent kept calling tools after being told to stop)")
        event.cancel_tool = f"Blocked: {why}. {advice}"

    def _check(self, event: BeforeToolCallEvent) -> None:
        name = str(event.tool_use.get("name"))
        key = (name, json.dumps(event.tool_use.get("input"), sort_keys=True, default=str))
        self._streak = self._streak + 1 if key == self._last else 1
        self._last = key
        if self.repeat_limit and self._streak > self.repeat_limit:
            self._block(event, f"{name} was called {self._streak} times in a row with identical arguments",
                        "Repeating it will not help. Try a different approach, or stop and report what you have.")
            return
        if self.max_tool_calls and self.calls >= self.max_tool_calls:
            self._block(event, f"tool-call budget of {self.max_tool_calls} is used up",
                        "Do not call any more tools. Reply now with the best result you have, and say what is missing.")
            return
        self.calls += 1
