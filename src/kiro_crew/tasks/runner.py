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

from strands import Agent

from ..agents.budget import find_budget_error
from ..core.context import AppContext
from ..core.logging import end_turn, start_turn
from ..core.types import Address
from . import store as S
from .verify import verify

logger = logging.getLogger(__name__)

Notify = Callable[[Address, str], Awaitable[None]]

TASK_PROMPT = (
    "You are executing a long-running background task autonomously. Work step by step, use tools, and call "
    "`report_progress` after each meaningful milestone (it is saved, so work can resume if interrupted). "
    "{ask}When finished, reply with the final deliverable only. If completion criteria are given, your result "
    "will be checked against them: make sure each one is visibly satisfied in your final reply."
)
ASK_ALLOWED = (
    "Make reasonable assumptions for small details and state them. If a wrong guess would waste significant work "
    "(a missing choice, conflicting requirements), call `ask_user` once with a clear question and your default "
    "assumption, then stop; the task resumes with the answer. "
)
ASK_FORBIDDEN = "Do not ask the user questions; make reasonable assumptions and state them. "

FIX_PROMPT = (
    "A reviewer checked your result against the completion criteria and found it NOT complete.\n"
    "Criteria: {criteria}\nProblem: {feedback}\n"
    "Fix this now (use tools if needed) and reply with the complete, corrected final deliverable."
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
                if self.store.expire_questions():
                    logger.info("Unanswered question(s) timed out; tasks continue with their stated defaults")
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
            ask = ASK_ALLOWED if self.cfg.ask_user and task.address else ASK_FORBIDDEN
            agent = factory.create(task.agent or self.cfg.agent, session_id=task.session_id, origin=task.address,
                                   user_id=task.user_id or "task", extra_prompt=TASK_PROMPT.format(ask=ask),
                                   task_id=task.id)
            prompt = self._prompt(task)
            if task.answer is not None:
                self.store.consume_answer(task.id)  # the answer now lives in the agent's saved conversation
            text, note = await asyncio.wait_for(self._work(task, agent, prompt), timeout=self.cfg.task_timeout_s)
            if text is None:  # the agent asked a question: parked as WAITING, no worker held, nothing to deliver
                logger.info("Task %s is waiting for the user's answer", task.id)
                return
            self.store.finish(task.id, S.SUCCEEDED, result=text)
            if task.quiet_token and text.upper().startswith(task.quiet_token.upper()):
                logger.info("Task %s finished quietly (%s)", task.id, task.quiet_token)
                return
            await self._tell(task, f"Task [{task.id}] done: {task.title}\n\n{note}{text}")
        except asyncio.CancelledError:
            current = self.store.get(task.id)
            if current and current.status == S.CANCELLED:
                await self._tell(task, f"Task [{task.id}] cancelled.")
            raise
        except Exception as e:
            if (stop := find_budget_error(e)) is not None:  # retrying would only burn the same budget again
                logger.warning("Task %s stopped: %s", task.id, stop)
                self.store.finish(task.id, S.FAILED, error=str(stop))
                await self._tell(task, f"Task [{task.id}] stopped: {stop}")
                return
            logger.exception("Task %s failed", task.id)
            err = (f"timed out after {self.cfg.task_timeout_s}s" if isinstance(e, TimeoutError)
                   else f"{type(e).__name__}: {e}")
            if task.attempts < self.cfg.max_attempts:
                self.store.requeue(task.id, delay_s=min(30 * 2 ** (task.attempts - 1), 600), error=err)
                self.wake()
            else:
                self.store.finish(task.id, S.FAILED, error=err)
                await self._tell(task, f"Task [{task.id}] failed after {task.attempts} attempts: {err}")

    def _prompt(self, task: S.Task) -> str:
        """What to tell the agent for this attempt: first run, answer to its question, or a retry."""
        criteria = (f"\n\nCompletion criteria (your result will be checked against these):\n{task.done_when}"
                    if task.done_when else "")
        if task.answer is not None:  # resuming after ask_user: the question is in the saved conversation
            return (f"The user's answer to your question ({task.question}):\n{task.answer}\n\n"
                    f"Continue the task to completion.\nGoal: {task.goal}{criteria}")
        if task.attempts <= 1:
            return f"Task: {task.title}\n\nGoal:\n{task.goal}{criteria}"
        progress = "\n".join(task.progress[-10:])
        if task.error and task.error.startswith("timed out"):
            self.store.add_progress(task.id, f"attempt {task.attempts}: retrying after a timeout")
            return (f"Your previous attempt ran out of time ({task.error}). Work more efficiently this time: skip "
                    "optional steps, report progress as you go, and deliver a partial result clearly marked as "
                    f"partial if you cannot finish.\nGoal: {task.goal}{criteria}\nProgress so far:\n{progress}")
        if task.error:
            self.store.add_progress(task.id, f"attempt {task.attempts}: retrying after: {task.error[:150]}")
            return (f"Your previous attempt FAILED with this error:\n{task.error[:500]}\n"
                    "Do not repeat the same approach. First state in one sentence what you will do differently "
                    "and why, then continue, reusing work that is already done.\n"
                    f"Goal: {task.goal}{criteria}\nProgress so far:\n{progress}")
        return ("You were interrupted. Review your previous work in this conversation and continue the task "
                f"to completion.\nGoal: {task.goal}{criteria}\nProgress so far:\n{progress}")

    def _parked(self, task: S.Task) -> bool:
        current = self.store.get(task.id)
        return current is not None and current.status == S.WAITING

    async def _work(self, task: S.Task, agent: Agent, prompt: str) -> tuple[str | None, str]:
        """Run the agent, then (when the task has criteria) verify and send it back to fix gaps.
        -> (text, note); text is None when the agent paused the task to ask the user something."""
        text = str(await agent.invoke_async(prompt)).strip() or "(no output)"
        if self._parked(task):
            return None, ""
        if task.quiet_token or not self.cfg.verify or not (task.done_when or self.cfg.verify_all):
            return text, ""
        criteria = task.done_when or "The result fully accomplishes the goal."
        fixes = 0
        while True:
            v = await verify(self.ctx, task.goal, criteria, text)
            if v.met:
                self.store.add_progress(task.id, "verified: completion criteria met")
                return text, ""
            if v.met is None:
                self.store.add_progress(task.id, f"verification inconclusive: {v.feedback}")
                return text, "(Completion could not be verified.)\n\n"
            self.store.add_progress(task.id, f"verification found a gap: {v.feedback}"[:300])
            if fixes >= self.cfg.max_verify_rounds:
                return text, f"WARNING - not confirmed complete after {fixes} fix round(s). Missing: {v.feedback}\n\n"
            fixes += 1
            text = str(await agent.invoke_async(
                FIX_PROMPT.format(criteria=criteria, feedback=v.feedback))).strip() or text
            if self._parked(task):
                return None, ""

    async def tell(self, task: S.Task, text: str) -> None:
        await self._tell(task, text)

    async def _tell(self, task: S.Task, text: str) -> None:
        if task.address:
            try:
                await self.notify(task.address, text)
            except Exception:
                logger.exception("Could not deliver task notification for %s", task.id)
