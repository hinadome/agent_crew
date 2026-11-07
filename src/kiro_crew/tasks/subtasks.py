"""Sub-tasks: a background task fans work out to child tasks that run in parallel, then continues with their results.

Waiting on children uses the same parked state as asking the user (``WAITING``, ``wait_kind='subtasks'``), so a parent
holds no worker while its children run (no deadlock with few workers), and the wait survives restarts. One level only:
sub-tasks cannot start their own. Children report to the parent, not to the chat.
"""

from __future__ import annotations

from typing import Any

from strands import ToolContext, tool

from ..agents.budget import PAUSED_KEY
from ..core.context import AppContext
from . import store as S

MAX_RESULT_CHARS = 3000


def results_text(children: list[S.Task]) -> str:
    parts = []
    for c in children:
        body = c.result if c.status == S.SUCCEEDED else (c.error or "(no details)")
        if body and len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + "\n[truncated]"
        parts.append(f"### [{c.id}] {c.title}: {c.status.upper()}\n{body or '(no output)'}")
    return "\n\n".join(parts) or "(no sub-tasks)"


def subtask_tools(ctx: AppContext, task_id: str) -> list[Any]:
    @tool(context=True)
    def start_subtask(title: str, goal: str, tool_context: ToolContext, done_when: str = "") -> str:
        """Start a sub-task that runs in parallel with other sub-tasks (for independent parts of the work, for
        example one topic each). Start all of them first, then call wait_for_subtasks. Sub-tasks cannot ask the user
        and cannot start sub-tasks.

        Args:
            title: Short label.
            goal: Complete, self-contained instructions: the sub-task does not see this conversation.
            done_when: Optional checkable completion criteria for the sub-task.
        """
        parent = ctx.tasks.get(task_id)
        if parent is None:
            return "Task not found."
        limit = ctx.config.tasks.max_subtasks
        if len(ctx.tasks.children(task_id)) >= limit:
            return f"Limit reached: a task may start at most {limit} sub-tasks. Do the rest yourself."
        child = ctx.tasks.create(title[:80], goal, agent=parent.agent, address=None, user_id=parent.user_id,
                                 done_when=done_when.strip() or None, parent_id=task_id)
        ctx.tasks.add_progress(task_id, f"started sub-task {child.id}: {title[:60]}")
        if (runner := ctx.services.get("task_runner")):
            runner.wake()
        return f"Started sub-task {child.id}."

    @tool(context=True)
    def wait_for_subtasks(tool_context: ToolContext) -> str:
        """Pause this task until all its sub-tasks have finished; you then continue with their results. After calling
        it, reply with one short line and stop."""
        kids = ctx.tasks.children(task_id)
        if not kids:
            return "There are no sub-tasks."
        if all(k.status in S.TERMINAL for k in kids):
            return "All sub-tasks are already finished:\n\n" + results_text(kids)
        if not ctx.tasks.wait_for_children(task_id):
            return "Could not pause; check later with task_status."
        tool_context.agent.state.set(PAUSED_KEY, True)
        return "Paused until the sub-tasks finish. Reply with the single line 'Waiting for sub-tasks.' and stop."

    return [start_subtask, wait_for_subtasks]
