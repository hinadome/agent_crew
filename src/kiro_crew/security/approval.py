"""Human-in-the-loop approval for sensitive tools.

An agent lists ``require_approval: [run_shell]``. When the model calls such a tool, the call is paused,
the user is asked in the same chat, and ``/approve`` or ``/deny`` resolves it. No answer within
``approval_timeout_s`` counts as a denial. The gateway lets ``/approve`` and ``/deny`` bypass the
per-conversation turn lock (the turn that is waiting holds it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from strands.hooks import BeforeToolCallEvent, HookRegistry

from ..core.types import Address

logger = logging.getLogger(__name__)
Send = Callable[[Address, str], Awaitable[None]]


class ApprovalManager:
    def __init__(self, timeout_s: int) -> None:
        self.timeout_s = timeout_s
        self.send: Send | None = None  # bound to Gateway.send once the gateway exists
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def has_pending(self, key: str) -> bool:
        return key in self._pending

    def resolve(self, key: str, approved: bool) -> bool:
        fut = self._pending.get(key)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True

    async def request(self, address: Address, description: str) -> bool:
        if self.send is None:
            return False
        key = address.key
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:  # at most one outstanding question per conversation
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._pending[key] = fut
            try:
                await self.send(address, f"Approval needed: {description}\nReply /approve or /deny "
                                         f"(auto-deny in {self.timeout_s}s).")
                return await asyncio.wait_for(fut, timeout=self.timeout_s)
            except TimeoutError:
                await self.send(address, "No answer - action denied.")
                return False
            finally:
                self._pending.pop(key, None)


class ApprovalHook:
    """Strands HookProvider that gates the named tools behind ``ApprovalManager``."""

    def __init__(self, manager: ApprovalManager, tool_names: set[str]) -> None:
        self.manager = manager
        self.tool_names = tool_names

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._check)

    async def _check(self, event: BeforeToolCallEvent) -> None:
        name = event.tool_use["name"]
        if name not in self.tool_names:
            return
        origin = event.agent.state.get("origin")
        if not origin:  # nobody to ask (e.g. scheduled/heartbeat turn without a target): fail closed
            event.cancel_tool = f"'{name}' requires approval but there is no chat to ask."
            return
        args = json.dumps(event.tool_use.get("input", {}), ensure_ascii=False)
        desc = f"run {name} with {args[:500]}{'...' if len(args) > 500 else ''}"
        if not await self.manager.request(Address(**origin), desc):
            event.cancel_tool = "The user did not approve this action."
