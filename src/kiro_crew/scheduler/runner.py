"""Scheduler loop: fires due schedules and maintains the heartbeat.

Firing modes
  message    send the stored text straight to the origin chat (no LLM; reliable reminders)
  task       enqueue a background task that runs the stored instructions with an agent
  heartbeat  like task, but a reply starting with the OK token is swallowed (nothing to report)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from ..core.context import AppContext
from ..core.logging import end_turn, log, start_turn
from ..core.types import Address
from .spec import parse_duration
from .store import Schedule

logger = logging.getLogger(__name__)
Notify = Callable[[Address, str], Awaitable[None]]

HEARTBEAT_ID = "heartbeat"
HEARTBEAT_PROMPT = (
    "This is a periodic heartbeat check. Review the checklist below using your tools and memory. "
    "If nothing needs the user's attention, reply with exactly {ok}. Otherwise reply with a short, "
    "actionable message for the user.\n\n<checklist>\n{checklist}\n</checklist>"
)
DEFAULT_CHECKLIST = "- Is there anything time-sensitive in my memory or open tasks that I should mention?\n"


class Scheduler:
    def __init__(self, ctx: AppContext, notify: Notify) -> None:
        self.ctx, self.notify = ctx, notify
        self.cfg = ctx.config.scheduler
        self.store = ctx.schedules
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        self._sync_heartbeat()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    # -- heartbeat registration -----------------------------------------------------------------
    def _sync_heartbeat(self) -> None:
        hb = self.ctx.config.heartbeat
        if not hb.enabled:
            self.store.delete(HEARTBEAT_ID)
            return
        if not hb.target or ":" not in hb.target:
            logger.warning("heartbeat.enabled but heartbeat.target is not '<channel>:<conversation_id>'; skipped")
            return
        channel, conv = hb.target.split(":", 1)
        secs = parse_duration(hb.every)
        checklist_path = self.ctx.workspace.root / "HEARTBEAT.md"
        if not checklist_path.exists():
            checklist_path.write_text("# Heartbeat checklist\n" + DEFAULT_CHECKLIST)
        existing = self.store.get(HEARTBEAT_ID)
        first = existing.next_run if existing and existing.expr == str(secs) and existing.next_run else time.time() + secs
        self.store.create(kind="every", expr=str(secs), first_run=first, mode="heartbeat", title="Heartbeat",
                          payload="", agent=hb.agent, address=Address(channel, conv), user_id=hb.user,
                          quiet_token=hb.ok_token, id=HEARTBEAT_ID)
        logger.info("Heartbeat every %s -> %s", hb.every, hb.target)

    # -- loop -----------------------------------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            try:
                for s in self.store.claim_due(self.cfg.timezone):
                    try:
                        await self.fire(s)
                    except Exception:
                        logger.exception("Schedule %s failed to fire; releasing lease for retry", s.id)
                        self.store.release_firing(s.id)
                    else:
                        # Advance only after the side effect was dispatched. A crash before this leaves the
                        # lease to expire and the schedule to be re-claimed later (at-least-once).
                        self.store.complete_firing(s.id, self.cfg.timezone)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.tick_s)
            except TimeoutError:
                pass

    async def fire(self, s: Schedule) -> None:
        token = start_turn("schedule", channel=s.address.channel if s.address else None,
                           conversation=s.address.conversation_id if s.address else None,
                           user=s.user_id, agent=s.agent)
        try:
            log(logger, logging.INFO, "firing schedule", id=s.id, mode=s.mode, title=s.title)
            if s.mode == "message":
                if s.address:
                    await self.notify(s.address, f"Reminder: {s.payload}")
                return
            prompt = s.payload
            if s.mode == "heartbeat":
                path = self.ctx.workspace.root / "HEARTBEAT.md"
                checklist = path.read_text() if path.exists() else DEFAULT_CHECKLIST
                prompt = HEARTBEAT_PROMPT.format(ok=s.quiet_token, checklist=checklist[:6000])
            self.ctx.tasks.create(s.title, prompt, agent=s.agent, address=s.address, user_id=s.user_id,
                                  quiet_token=s.quiet_token)
            if (runner := self.ctx.services.get("task_runner")) is not None:
                runner.wake()
        finally:
            end_turn(token)
