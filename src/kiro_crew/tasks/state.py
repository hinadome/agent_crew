"""Structured per-task state: a plan (steps with status) and a notes scratchpad.

Both live in the task row, so they survive crashes, retries and conversation summarisation. ``TaskStateHook``
re-renders them into the task agent's system prompt before *every* model call, so the agent always sees the
current plan even after old messages were summarised away.
"""

from __future__ import annotations

from typing import Any

from strands import tool
from strands.hooks import BeforeModelCallEvent, HookRegistry

from ..core.context import AppContext

STATUSES = ("pending", "doing", "done", "skipped", "blocked")
MAX_STEPS = 30
MAX_STEP_CHARS = 300
MAX_NOTES = 40
MAX_NOTE_CHARS = 2000
MAX_NOTES_TOTAL = 8000


def render_state(plan: list[dict[str, str]], notes: dict[str, str]) -> str:
    if not plan and not notes:
        return ("<task_state>\nNo plan yet. For multi-step work call update_plan first; record findings you will need "
                "later with save_note.\n</task_state>")
    lines = ["<task_state>"]
    if plan:
        lines.append("Plan:")
        for i, p in enumerate(plan, 1):
            note = f" - {p['note']}" if p.get("note") else ""
            lines.append(f"{i}. [{p.get('status', 'pending')}] {p['step']}{note}")
    if notes:
        lines.append("Notes:")
        lines += [f"- {k}: {v}" for k, v in notes.items()]
    lines.append("</task_state>")
    return "\n".join(lines)


class TaskStateHook:
    """Keeps the task agent's system prompt = base prompt + the current, persisted task state."""

    def __init__(self, ctx: AppContext, task_id: str, base_prompt: str) -> None:
        self.ctx, self.task_id, self.base_prompt = ctx, task_id, base_prompt

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._refresh)

    def _refresh(self, event: BeforeModelCallEvent) -> None:
        t = self.ctx.tasks.get(self.task_id)
        if t is not None:
            event.agent.system_prompt = f"{self.base_prompt}\n\n{render_state(t.plan, t.notes)}"


def task_state_tools(ctx: AppContext, task_id: str) -> list[Any]:
    def _task() -> Any:
        t = ctx.tasks.get(task_id)
        if t is None:
            raise ValueError("task not found")
        return t

    @tool
    def update_plan(steps: list[str]) -> str:
        """Set or revise the plan for this task: the ordered list of steps to reach the goal. Steps whose text is
        unchanged keep their status. Call it at the start of multi-step work and whenever the plan changes.

        Args:
            steps: Short, concrete steps in order (at most 30).
        """
        clean = [s.strip()[:MAX_STEP_CHARS] for s in steps if s and s.strip()]
        if not clean:
            return "Give at least one step."
        if len(clean) > MAX_STEPS:
            return f"Too many steps ({len(clean)}); merge them into at most {MAX_STEPS}."
        old = {p["step"]: p for p in _task().plan}
        plan = [{"step": s, "status": old.get(s, {}).get("status", "pending"), "note": old.get(s, {}).get("note", "")}
                for s in clean]
        ctx.tasks.set_plan(task_id, plan)
        return render_state(plan, _task().notes)

    @tool
    def mark_step(number: int, status: str, note: str = "") -> str:
        """Update one step of the plan.

        Args:
            number: The step number as shown in the plan (1-based).
            status: One of pending, doing, done, skipped, blocked.
            note: Optional short result or reason (for example where the output was saved, or why it is blocked).
        """
        t = _task()
        if not t.plan:
            return "There is no plan yet; call update_plan first."
        if not 1 <= number <= len(t.plan):
            return f"No step {number}; the plan has {len(t.plan)} steps."
        if status not in STATUSES:
            return f"Unknown status {status!r}; use one of: {', '.join(STATUSES)}."
        plan = t.plan
        plan[number - 1] = {**plan[number - 1], "status": status, "note": note.strip()[:MAX_STEP_CHARS]}
        ctx.tasks.set_plan(task_id, plan)
        if status in ("done", "blocked"):
            ctx.tasks.add_progress(task_id, f"step {number} {status}: {plan[number - 1]['step']}")
        return render_state(plan, t.notes)

    @tool
    def save_note(key: str, value: str) -> str:
        """Save a finding you will need later (a URL, a number, a decision, a file path). Notes survive interruptions
        and are shown to you on every step. Saving an empty value deletes the note.

        Args:
            key: Short name, for example "best_flight" or "sources".
            value: The content (at most 2000 characters).
        """
        key = key.strip()[:60]
        if not key:
            return "Give the note a key."
        notes = _task().notes
        if not value.strip():
            notes.pop(key, None)
        else:
            if key not in notes and len(notes) >= MAX_NOTES:
                return f"Too many notes ({MAX_NOTES}); delete or merge some first."
            candidate = {**notes, key: value.strip()[:MAX_NOTE_CHARS]}
            if sum(len(k) + len(v) for k, v in candidate.items()) > MAX_NOTES_TOTAL:
                return f"Notes would exceed {MAX_NOTES_TOTAL} characters; shorten or delete some first."
            notes = candidate
        ctx.tasks.set_notes(task_id, notes)
        return "Saved." if value.strip() else "Deleted."

    return [update_plan, mark_step, save_note]
