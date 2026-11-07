"""Agent-facing task tools: delegate long work to the background queue and check on it."""

from __future__ import annotations

import time
from typing import Any

from strands import ToolContext, tool

from ..agents.budget import PAUSED_KEY
from ..core.context import AppContext
from ..core.types import Address
from ..tools import tool_factories


@tool_factories.register("tasks")
def _tasks(ctx: AppContext) -> Any:
    @tool(context=True)
    def start_task(title: str, goal: str, tool_context: ToolContext, done_when: str = "") -> str:
        """Start a long-running background task (research, multi-step coding, large analysis...).
        It runs independently; the user is notified in this chat when it finishes. Use this instead
        of doing slow multi-step work inline. Write the goal so it is fully self-contained.

        Args:
            title: Short label (under 60 chars).
            goal: Complete, self-contained instructions including desired deliverable format.
            done_when: Optional, concrete and checkable completion criteria (for example "a file report.md exists
                with at least 3 sources" or "the answer states the price in EUR"). The result is verified against
                it and the agent is sent back to fix gaps, so state what "done" means.
        """
        st = tool_context.agent.state
        origin = st.get("origin")
        addr = Address(**origin) if origin else None
        t = ctx.tasks.create(title, goal, agent=st.get("agent_name"), address=addr, user_id=st.get("user_id"),
                           done_when=done_when.strip() or None)
        if (runner := ctx.services.get("task_runner")):
            runner.wake()
        return f"Started task {t.id}. The user will be notified on completion."

    @tool(context=True)
    def list_tasks(tool_context: ToolContext) -> str:
        """List this user's recent background tasks and their status."""
        tasks = ctx.tasks.list(user_id=tool_context.agent.state.get("user_id"), limit=15)
        return "\n".join(t.summary() for t in tasks) or "No tasks."

    @tool
    def task_status(task_id: str) -> str:
        """Get status, progress notes and result of a background task.

        Args:
            task_id: The task id.
        """
        t = ctx.tasks.get(task_id)
        if not t:
            return "No such task."
        plan = "\n".join(f"{i}. [{p['status']}] {p['step']}" for i, p in enumerate(t.plan, 1))
        return (f"{t.summary()}\n" + (f"Plan:\n{plan}\n" if plan else "") + "Progress:\n" + "\n".join(t.progress[-10:])
                + (f"\nResult:\n{t.result}" if t.result else ""))

    @tool
    def cancel_task(task_id: str) -> str:
        """Cancel a queued or running background task.

        Args:
            task_id: The task id.
        """
        runner = ctx.services.get("task_runner")
        ok = runner.cancel(task_id) if runner else ctx.tasks.cancel(task_id)
        return "Cancelled." if ok else "Task not found or already finished."

    return [start_task, list_tasks, task_status, cancel_task]


def ask_user_tool(ctx: AppContext, task_id: str) -> Any:
    """Per-task tool: pause the task and ask the user a question (answered with /answer <id> <text>)."""

    @tool(context=True)
    async def ask_user(question: str, default_assumption: str, tool_context: ToolContext) -> str:
        """Ask the user ONE question and pause this task until they answer. Use it only when a wrong guess would
        waste significant work (a missing choice, a conflicting requirement), never for details you can reasonably
        assume. After calling it, reply with one short line and stop; the task resumes with the answer.

        Args:
            question: One clear, self-contained question, with the options if there are any.
            default_assumption: What you will assume if the user does not answer in time.
        """
        cfg = ctx.config.tasks
        task = ctx.tasks.get(task_id)
        if task is None or task.address is None:
            return f"There is nobody to ask. Assume: {default_assumption}. State this assumption in your result."
        if task.asks >= cfg.max_questions:
            return (f"You have already asked {task.asks} questions; no more are allowed. Decide yourself, "
                    f"state your assumptions in the result, and continue.")
        deadline = time.time() + cfg.ask_timeout_s
        if not ctx.tasks.ask(task_id, question.strip(), default_assumption.strip(), deadline):
            return "Could not pause the task; decide yourself and continue."
        tool_context.agent.state.set(PAUSED_KEY, True)  # BudgetHook now blocks every further tool call
        mins = max(1, cfg.ask_timeout_s // 60)
        if (runner := ctx.services.get("task_runner")):
            await runner.tell(task, f"Task [{task_id}] needs your input: {question.strip()}\n"
                                    f"Reply: /answer {task_id} <your answer>\n"
                                    f"(No answer within {mins} min: I will assume {default_assumption.strip()})")
        return ("Question sent. This task is now paused until the user answers. Reply with the single line "
                "'Waiting for the user's answer.' and call no more tools.")

    return ask_user


def report_progress_tool(ctx: AppContext, task_id: str) -> Any:
    """Per-task tool (bound to one task id): persists milestones and pings the user."""

    @tool
    def report_progress(note: str) -> str:
        """Record a milestone for the current background task (saved; visible to the user via status).

        Args:
            note: One short sentence describing what was just completed.
        """
        ctx.tasks.add_progress(task_id, note)
        return "Progress saved."

    return report_progress
