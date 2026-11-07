"""Guard rails for autonomous runs: a tool-call budget and repeated-call (stuck loop) detection.

Both first *steer* the model (the tool call is cancelled and the model is told why, so it can change course or
wrap up), and only stop the run hard (``BudgetExceeded``) if it keeps ignoring the message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from strands.hooks import BeforeInvocationEvent, BeforeModelCallEvent, BeforeToolCallEvent, HookRegistry

from ..core.logging import log

logger = logging.getLogger("kiro_crew.agents.budget")


class BudgetExceeded(RuntimeError):
    """The agent ignored repeated warnings and was stopped."""


class TaskPaused(RuntimeError):
    """The task asked the user a question and the agent kept working instead of stopping; end the run now."""


PAUSED_KEY = "paused_for_user"  # agent.state flag set by ask_user
MAX_CALLS_WHILE_PAUSED = 3
HARD_TOKEN_FACTOR = 1.25  # soft limit at 100% (tools refused), hard stop at 125%


def tokens_used(agent: Any) -> int:
    try:
        return int(agent.event_loop_metrics.accumulated_usage.get("totalTokens", 0))
    except Exception:  # noqa: BLE001 - metrics are best effort
        return 0


def _find(exc: BaseException | None, kind: type[Exception]) -> Any:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, kind):
            return exc
        seen.add(id(exc))
        exc = getattr(exc, "original_exception", None) or exc.__cause__
    return None


def find_budget_error(exc: BaseException | None) -> BudgetExceeded | None:
    """Strands wraps hook errors (EventLoopException); dig the BudgetExceeded out of the chain."""
    found: BudgetExceeded | None = _find(exc, BudgetExceeded)
    return found


def find_pause(exc: BaseException | None) -> TaskPaused | None:
    found: TaskPaused | None = _find(exc, TaskPaused)
    return found


class BudgetHook:
    def __init__(self, max_tool_calls: int, repeat_limit: int, grace_calls: int = 3, *,
                 per_invocation: bool = True, max_tokens: int = 0, prior_tokens: int = 0) -> None:
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.prior_tokens = prior_tokens  # already spent by earlier attempts of the same task
        self._token_base = 0
        self.repeat_limit = repeat_limit
        self.grace_calls = grace_calls
        self.per_invocation = per_invocation  # False: one budget for the object's whole life (task runs)
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.blocked = 0
        self._last: tuple[str, str] | None = None
        self._streak = 0
        self._paused_calls = 0

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._begin)
        registry.add_callback(BeforeToolCallEvent, self._check)
        registry.add_callback(BeforeModelCallEvent, self._check_model)

    def _begin(self, event: BeforeInvocationEvent) -> None:
        if self.per_invocation:
            self.reset()
            self._token_base = tokens_used(event.agent)  # chat agents are cached: count this turn only

    def used(self, agent: Any) -> int:
        return tokens_used(agent) - self._token_base + self.prior_tokens

    def _check_model(self, event: BeforeModelCallEvent) -> None:
        if self.max_tokens and (used := self.used(event.agent)) > self.max_tokens * HARD_TOKEN_FACTOR:
            raise BudgetExceeded(f"token budget of {self.max_tokens} used up ({used} tokens)")

    def _block(self, event: BeforeToolCallEvent, why: str, advice: str) -> None:
        self.blocked += 1
        name = event.tool_use.get("name")
        log(logger, logging.WARNING, "tool call blocked", tool=name, reason=why, blocked=self.blocked)
        if self.blocked > self.grace_calls:
            raise BudgetExceeded(f"stopped: {why} (the agent kept calling tools after being told to stop)")
        event.cancel_tool = f"Blocked: {why}. {advice}"

    def _check(self, event: BeforeToolCallEvent) -> None:
        name = str(event.tool_use.get("name"))
        if event.agent.state.get(PAUSED_KEY):  # the task is waiting for the user: no more work until they answer
            self._paused_calls += 1
            if self._paused_calls > MAX_CALLS_WHILE_PAUSED:
                raise TaskPaused("task is paused waiting for the user's answer")
            event.cancel_tool = ("Blocked: this task is paused until the user answers your question. "
                                 "Reply with the single line 'Waiting for the user's answer.' and stop.")
            return
        key = (name, json.dumps(event.tool_use.get("input"), sort_keys=True, default=str))
        self._streak = self._streak + 1 if key == self._last else 1
        self._last = key
        if self.repeat_limit and self._streak > self.repeat_limit:
            self._block(event, f"{name} was called {self._streak} times in a row with identical arguments",
                        "Repeating it will not help. Try a different approach, or stop and report what you have.")
            return
        if self.max_tokens and (used := self.used(event.agent)) >= self.max_tokens:
            self._block(event, f"token budget of {self.max_tokens} is used up ({used} tokens)",
                        "Do not call any more tools. Reply now with the best result you have, and say what is missing.")
            return
        if self.max_tool_calls and self.calls >= self.max_tool_calls:
            self._block(event, f"tool-call budget of {self.max_tool_calls} is used up",
                        "Do not call any more tools. Reply now with the best result you have, and say what is missing.")
            return
        self.calls += 1
