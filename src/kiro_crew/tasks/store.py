"""Durable task queue. Source of truth for long-running work -- survives restarts and crashes."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.types import Address
from ..memory.db import Database

QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED = "queued", "running", "succeeded", "failed", "cancelled"
TERMINAL = {SUCCEEDED, FAILED, CANCELLED}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
    agent TEXT, channel TEXT, conversation_id TEXT, thread_id TEXT, user_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, progress TEXT NOT NULL DEFAULT '[]',
    result TEXT, error TEXT, not_before REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, not_before);
"""


@dataclass
class Task:
    id: str
    title: str
    goal: str
    status: str
    agent: str | None
    address: Address | None
    user_id: str | None
    attempts: int
    progress: list[str] = field(default_factory=list)
    result: str | None = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    quiet_token: str | None = None  # a result starting with this is not delivered (e.g. HEARTBEAT_OK)
    done_when: str | None = None  # completion criteria the result is verified against

    @property
    def session_id(self) -> str:
        return f"task-{self.id}"

    def summary(self) -> str:
        last = f" | last: {self.progress[-1]}" if self.progress else ""
        return f"[{self.id}] {self.status.upper()} - {self.title}{last}"


def _row_to_task(r: Any) -> Task:
    addr = Address(r["channel"], r["conversation_id"], r["thread_id"]) if r["channel"] else None
    return Task(r["id"], r["title"], r["goal"], r["status"], r["agent"], addr, r["user_id"], r["attempts"],
                json.loads(r["progress"]), r["result"], r["error"], r["created_at"], r["updated_at"],
                r["quiet_token"], r["done_when"])


class TaskStore:
    MAX_PROGRESS = 50  # keep only the latest notes

    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(_SCHEMA)
        db.ensure_column("tasks", "quiet_token", "TEXT")  # added after v0.1: upgrade older databases
        db.ensure_column("tasks", "done_when", "TEXT")

    def create(self, title: str, goal: str, *, agent: str | None, address: Address | None,
               user_id: str | None, quiet_token: str | None = None, done_when: str | None = None) -> Task:
        tid, now = uuid.uuid4().hex[:8], time.time()
        self.db.execute(
            "INSERT INTO tasks(id,title,goal,status,agent,channel,conversation_id,thread_id,user_id,"
            "created_at,updated_at,quiet_token,done_when) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, goal, QUEUED, agent, address and address.channel, address and address.conversation_id,
             address and address.thread_id, user_id, now, now, quiet_token, done_when))
        return self.get(tid)  # type: ignore[return-value]

    def get(self, task_id: str) -> Task | None:
        r = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return _row_to_task(r) if r else None

    def list(self, *, user_id: str | None = None, active_only: bool = False, limit: int = 20) -> list[Task]:
        sql, params = "SELECT * FROM tasks WHERE 1=1", []
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if active_only:
            sql += " AND status IN ('queued','running')"
        rows = self.db.query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, limit])
        return [_row_to_task(r) for r in rows]

    def claim_next(self) -> Task | None:
        """Atomically move the oldest runnable queued task to running."""
        now = time.time()
        with self.db.transaction():
            r = self.db.one("SELECT id FROM tasks WHERE status=? AND not_before<=? ORDER BY created_at LIMIT 1",
                            (QUEUED, now))
            if not r:
                return None
            self.db.execute("UPDATE tasks SET status=?, attempts=attempts+1, updated_at=? WHERE id=?",
                            (RUNNING, now, r["id"]))
        return self.get(r["id"])

    def add_progress(self, task_id: str, note: str) -> None:
        t = self.get(task_id)
        if not t:
            return
        notes = [*t.progress, f"{time.strftime('%H:%M:%S')} {note.strip()}"][-self.MAX_PROGRESS:]
        self.db.execute("UPDATE tasks SET progress=?, updated_at=? WHERE id=?",
                        (json.dumps(notes), time.time(), task_id))

    def finish(self, task_id: str, status: str, *, result: str | None = None, error: str | None = None) -> None:
        # Never overwrite a terminal state (e.g. user cancelled while the agent was finishing).
        self.db.execute("UPDATE tasks SET status=?, result=?, error=?, updated_at=? WHERE id=? "
                        "AND status NOT IN ('succeeded','failed','cancelled')",
                        (status, result, error, time.time(), task_id))

    def requeue(self, task_id: str, *, delay_s: float = 0, error: str | None = None) -> None:
        self.db.execute("UPDATE tasks SET status=?, not_before=?, error=?, updated_at=? WHERE id=? AND status=?",
                        (QUEUED, time.time() + delay_s, error, time.time(), task_id, RUNNING))

    def recover_interrupted(self) -> int:
        """On startup: tasks left 'running' by a crash go back to the queue; they resume from their session."""
        return self.db.execute("UPDATE tasks SET status=?, updated_at=? WHERE status=?",
                               (QUEUED, time.time(), RUNNING)).rowcount

    def active_session_ids(self) -> set[str]:
        """Session ids of queued/running tasks (housekeeping must never prune these)."""
        return {f"task-{r['id']}" for r in self.db.query("SELECT id FROM tasks WHERE status IN ('queued','running')")}

    def purge_finished(self, older_than_s: float) -> int:
        return self.db.execute("DELETE FROM tasks WHERE status IN ('succeeded','failed','cancelled') "
                               "AND updated_at < ?", (time.time() - older_than_s,)).rowcount

    def cancel(self, task_id: str) -> bool:
        return self.db.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status IN ('queued','running')",
                               (CANCELLED, time.time(), task_id)).rowcount > 0
