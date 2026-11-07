"""Background workers that execute queued tasks with a dedicated, resumable agent session.

Why this holds up for long work:
- Each task has its own Strands session (``task-<id>``), persisted message-by-message. After a crash
  or restart the task is re-queued and the agent *continues from its saved history*.
- Progress is written to the task row (and echoed to the user), so work is observable.
- Per-attempt timeout, bounded retries with exponential backoff, and user cancellation.
- The task agent has its own context window; it never bloats the chat that spawned it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..core.context import AppContext
from ..core.logging import end_turn, start_turn
from ..core.types import Address
from . import store as S

logger = logging.getLogger(__name__)

Notify = Callable[[Address, str], Awaitable[None]]

TASK_PROMPT = (
    "You are executing a long-running background task autonomously. Work step by step, use tools, and call "
    "`report_progress` after each meaningful milestone (it is saved, so work can resume if interrupted). "
    "Do not ask the user questions; make reasonable assumptions and state them. When finished, reply with the "
    "final deliverable only."
)


class TaskRunner:
    def __init__(self, ctx: AppContext, notify: Notify) -> None:
        self.ctx = ctx
        self.cfg = ctx.config.tasks
        self.store = ctx.tasks
        self.notify = notify
        self._workers: list[asyncio.Task[None]] = []
        self._running: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._stopping = False

    def start(self) -> None:
        n = self.store.recover_interrupted()
        if n:
            logger.info("Re-queued %d interrupted task(s)", n)
        self._workers = [asyncio.create_task(self._loop(i), name=f"task-worker-{i}")
                         for i in range(self.cfg.workers)]

    async def stop(self) -> None:
        self._stopping = True
        for t in [*self._workers, *self._running.values()]:
            t.cancel()
        await asyncio.gather(*self._workers, *self._running.values(), return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    def cancel(self, task_id: str) -> bool:
        ok = self.store.cancel(task_id)
        if (t := self._running.get(task_id)) is not None:
            t.cancel()
        return ok

    async def _loop(self, idx: int) -> None:
        while not self._stopping:
            try:
                task = self.store.claim_next()
                if task is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=5)
                    except TimeoutError:
                        pass
                    continue
                run = asyncio.create_task(self._run(task))
                self._running[task.id] = run
                try:
                    await run
                except asyncio.CancelledError:
                    if self._stopping:
                        raise  # shutting down: leave status 'running' so it is recovered next start
                finally:
                    self._running.pop(task.id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task worker %d crashed; continuing", idx)
                await asyncio.sleep(2)

    async def _run(self, task: S.Task) -> None:
        kind = "heartbeat" if task.quiet_token else "task"
        token = start_turn(kind, channel=task.address.channel if task.address else None,
                           conversation=task.address.conversation_id if task.address else None,
                           user=task.user_id, agent=task.agent or self.cfg.agent)
        try:
            await self._run_inner(task)
        finally:
            end_turn(token)

    async def _run_inner(self, task: S.Task) -> None:
        logger.info("Task %s attempt %d: %s", task.id, task.attempts, task.title)
        try:
            factory = self.ctx.services["agent_factory"]
            agent = factory.create(task.agent or self.cfg.agent, session_id=task.session_id, origin=task.address,
                                   user_id=task.user_id or "task", extra_prompt=TASK_PROMPT, task_id=task.id)
            prompt = (f"Task: {task.title}\n\nGoal:\n{task.goal}" if task.attempts <= 1 else
                      "You were interrupted. Review your previous work in this conversation and continue the task "
                      f"to completion.\nGoal: {task.goal}\nProgress so far:\n" + "\n".join(task.progress[-10:]))
            result = await asyncio.wait_for(agent.invoke_async(prompt), timeout=self.cfg.task_timeout_s)
            text = str(result).strip() or "(no output)"
            self.store.finish(task.id, S.SUCCEEDED, result=text)
            if task.quiet_token and text.upper().startswith(task.quiet_token.upper()):
                logger.info("Task %s finished quietly (%s)", task.id, task.quiet_token)
                return
            await self._tell(task, f"Task [{task.id}] done: {task.title}\n\n{text}")
        except asyncio.CancelledError:
            current = self.store.get(task.id)
            if current and current.status == S.CANCELLED:
                await self._tell(task, f"Task [{task.id}] cancelled.")
            raise
        except Exception as e:
            logger.exception("Task %s failed", task.id)
            err = (f"timed out after {self.cfg.task_timeout_s}s" if isinstance(e, TimeoutError)
                   else f"{type(e).__name__}: {e}")
            if task.attempts < self.cfg.max_attempts:
                self.store.requeue(task.id, delay_s=min(30 * 2 ** (task.attempts - 1), 600), error=err)
                self.wake()
            else:
                self.store.finish(task.id, S.FAILED, error=err)
                await self._tell(task, f"Task [{task.id}] failed after {task.attempts} attempts: {err}")

    async def _tell(self, task: S.Task, text: str) -> None:
        if task.address:
            try:
                await self.notify(task.address, text)
            except Exception:
                logger.exception("Could not deliver task notification for %s", task.id)
