"""Agent-facing task tools: delegate long work to the background queue and check on it."""

from __future__ import annotations

from typing import Any

from strands import ToolContext, tool

from ..core.context import AppContext
from ..core.types import Address
from ..tools import tool_factories


@tool_factories.register("tasks")
def _tasks(ctx: AppContext) -> Any:
    @tool(context=True)
    def start_task(title: str, goal: str, tool_context: ToolContext) -> str:
        """Start a long-running background task (research, multi-step coding, large analysis...).
        It runs independently; the user is notified in this chat when it finishes. Use this instead
        of doing slow multi-step work inline. Write the goal so it is fully self-contained.

        Args:
            title: Short label (under 60 chars).
            goal: Complete, self-contained instructions including desired deliverable format.
        """
        st = tool_context.agent.state
        origin = st.get("origin")
        addr = Address(**origin) if origin else None
        t = ctx.tasks.create(title, goal, agent=st.get("agent_name"), address=addr, user_id=st.get("user_id"))
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
        return f"{t.summary()}\nProgress:\n" + "\n".join(t.progress[-10:]) + (f"\nResult:\n{t.result}" if t.result else "")

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
