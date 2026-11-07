"""Agent-facing scheduling tools (tool group ``schedule``)."""

from __future__ import annotations

from typing import Any

from strands import ToolContext, tool

from ..core.context import AppContext
from ..core.types import Address
from ..tools import tool_factories
from .spec import ScheduleError, parse_when

WHEN_HELP = ("When: 'in 30m', 'at 2026-11-07 09:00', 'every 2h', or 'cron 0 9 * * 1-5' "
             "(times use the configured timezone).")


@tool_factories.register("schedule")
def _schedule(ctx: AppContext) -> Any:
    tz = ctx.config.scheduler.timezone

    def _create(tool_context: ToolContext, when: str, mode: str, title: str, payload: str) -> str:
        st = tool_context.agent.state
        owner, origin = st.get("user_id"), st.get("origin")
        if ctx.schedules.count(owner) >= ctx.config.scheduler.max_jobs_per_user:
            return f"Too many schedules (limit {ctx.config.scheduler.max_jobs_per_user}). Cancel some first."
        try:
            p = parse_when(when, tz)
        except ScheduleError as e:
            return f"Could not schedule: {e}"
        sched = ctx.schedules.create(kind=p.kind, expr=p.expr, first_run=p.next_run, mode=mode, title=title,
                                     payload=payload, agent=st.get("agent_name"),
                                     address=Address(**origin) if origin else None, user_id=owner)
        if (s := ctx.services.get("scheduler")) is not None:
            s.wake()
        return f"Scheduled. {sched.summary()}"

    @tool(context=True)
    def remind_me(when: str, text: str, tool_context: ToolContext) -> str:
        """Send the user a plain reminder message in this chat at a later time. No AI work is done at
        that time; the text is delivered as written.

        Args:
            when: Schedule. Examples: 'in 30m', 'at 2026-11-07 09:00', 'every 1d', 'cron 0 9 * * 1-5'.
            text: The reminder text to deliver.
        """
        return _create(tool_context, when, "message", text[:60], text)

    @tool(context=True)
    def schedule_job(when: str, title: str, instructions: str, tool_context: ToolContext) -> str:
        """Schedule a job for an AI agent to carry out later or repeatedly (for example a daily summary).
        At the scheduled time it runs as a background task and the result is sent to this chat.

        Args:
            when: Schedule. Examples: 'in 2h', 'at 2026-11-07 09:00', 'every 1d', 'cron 0 9 * * 1-5'.
            title: Short label.
            instructions: Complete, self-contained instructions for the job, including the desired output.
        """
        return _create(tool_context, when, "task", title, instructions)

    @tool(context=True)
    def list_schedules(tool_context: ToolContext) -> str:
        """List this user's active reminders and scheduled jobs."""
        items = ctx.schedules.list(tool_context.agent.state.get("user_id"))
        return "\n".join(s.summary() for s in items) or "No schedules."

    @tool(context=True)
    def cancel_schedule(schedule_id: str, tool_context: ToolContext) -> str:
        """Cancel a reminder or scheduled job.

        Args:
            schedule_id: The id shown by list_schedules.
        """
        ok = ctx.schedules.delete(schedule_id, tool_context.agent.state.get("user_id"))
        return "Cancelled." if ok else "No such schedule."

    return [remind_me, schedule_job, list_schedules, cancel_schedule]
