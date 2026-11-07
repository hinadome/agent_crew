"""Housekeeping: keep the stored data from growing forever (storage-backend agnostic)."""

from __future__ import annotations

import asyncio
import logging

from ..core.context import AppContext
from ..storage import SessionStore

logger = logging.getLogger(__name__)
DAY = 86400


def prune_sessions(sessions: SessionStore, retention_days: int, protected: set[str] | None = None) -> int:
    """Delete conversations idle for longer than ``retention_days`` (0 keeps forever), sparing ``protected`` ids
    (the sessions of queued/running tasks)."""
    if retention_days <= 0:
        return 0
    return sessions.prune_idle(retention_days * DAY, protected or set())


def run_once(ctx: AppContext) -> dict[str, int]:
    cfg = ctx.config.housekeeping
    result = {"tasks": 0, "sessions": 0}
    if cfg.task_retention_days > 0:
        result["tasks"] = ctx.tasks.purge_finished(cfg.task_retention_days * DAY)
    result["sessions"] = prune_sessions(ctx.storage.sessions, cfg.session_retention_days,
                                        ctx.tasks.active_session_ids())
    return result


class Housekeeper:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.ctx.config.housekeeping.enabled:
            self._task = asyncio.create_task(self._loop(), name="housekeeper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        interval = self.ctx.config.housekeeping.interval_h * 3600
        while True:
            try:
                res = await asyncio.to_thread(run_once, self.ctx)
                if any(res.values()):
                    logger.info("Housekeeping removed %s", res)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("housekeeping failed")
            await asyncio.sleep(interval)
