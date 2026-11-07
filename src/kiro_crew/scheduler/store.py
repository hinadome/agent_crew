"""Durable schedules (reminders, recurring jobs, heartbeat)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..core.types import Address
from ..memory.db import Database
from .spec import describe, next_run

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, expr TEXT NOT NULL, mode TEXT NOT NULL,
    title TEXT NOT NULL, payload TEXT NOT NULL, agent TEXT,
    channel TEXT, conversation_id TEXT, thread_id TEXT, user_id TEXT,
    enabled INTEGER NOT NULL DEFAULT 1, next_run REAL, last_run REAL, run_count INTEGER NOT NULL DEFAULT 0,
    quiet_token TEXT, firing_until REAL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_run);
"""

_List = list  # ScheduleStore defines a method named `list`; annotate with this alias instead

MODES = ("message", "task", "heartbeat")  # message: send text as-is | task/heartbeat: run an agent


@dataclass
class Schedule:
    id: str
    kind: str
    expr: str
    mode: str
    title: str
    payload: str
    agent: str | None
    address: Address | None
    user_id: str | None
    enabled: bool
    next_run: float | None
    last_run: float | None
    run_count: int
    quiet_token: str | None

    def summary(self) -> str:
        nxt = (time.strftime("%Y-%m-%d %H:%M", time.localtime(self.next_run))
               if self.enabled and self.next_run else "-")
        return f"[{self.id}] {describe(self.kind, self.expr)} | next {nxt} | {self.mode}: {self.title}"


def _row(r: Any) -> Schedule:
    addr = Address(r["channel"], r["conversation_id"], r["thread_id"]) if r["channel"] else None
    return Schedule(r["id"], r["kind"], r["expr"], r["mode"], r["title"], r["payload"], r["agent"], addr,
                    r["user_id"], bool(r["enabled"]), r["next_run"], r["last_run"], r["run_count"], r["quiet_token"])


class ScheduleStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(_SCHEMA)
        db.ensure_column("schedules", "firing_until", "REAL")  # added after v0.2 for at-least-once leasing

    def create(self, *, kind: str, expr: str, first_run: float, mode: str, title: str, payload: str,
               agent: str | None, address: Address | None, user_id: str | None,
               quiet_token: str | None = None, id: str | None = None) -> Schedule:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        sid = id or uuid.uuid4().hex[:8]
        self.db.execute(
            "INSERT OR REPLACE INTO schedules(id,kind,expr,mode,title,payload,agent,channel,conversation_id,"
            "thread_id,user_id,enabled,next_run,quiet_token,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (sid, kind, expr, mode, title, payload, agent, address and address.channel,
             address and address.conversation_id, address and address.thread_id, user_id, first_run, quiet_token,
             time.time()))
        return self.get(sid)  # type: ignore[return-value]

    def get(self, sid: str) -> Schedule | None:
        r = self.db.one("SELECT * FROM schedules WHERE id=?", (sid,))
        return _row(r) if r else None

    def list(self, user_id: str | None = None, include_system: bool = False) -> _List[Schedule]:
        sql, params = "SELECT * FROM schedules WHERE enabled=1", []
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if not include_system:
            sql += " AND id NOT IN ('heartbeat')"
        return [_row(r) for r in self.db.query(sql + " ORDER BY next_run", params)]

    def count(self, user_id: str | None) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM schedules WHERE enabled=1 AND user_id IS ?", (user_id,)))

    def delete(self, sid: str, user_id: str | None = None) -> bool:
        sql, params = "DELETE FROM schedules WHERE id=?", [sid]
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        return self.db.execute(sql, params).rowcount > 0

    def claim_due(self, tz: str, now: float | None = None, lease_s: float = 300) -> _List[Schedule]:
        """Lease due schedules for firing (at-least-once).

        A due schedule is marked ``firing_until = now + lease_s`` and returned, but ``next_run`` is NOT
        advanced yet: that happens in :meth:`complete_firing` once the firing side effect has been dispatched.
        If the process dies between claim and completion, the lease expires and the schedule is re-claimed on a
        later tick, so a firing is never silently dropped. A due schedule whose previous lease is still active
        is skipped (another worker/tick owns it). The trade-off versus the old at-most-once behaviour: a crash
        after the side effect but before completion can fire a job twice. ``message`` reminders may duplicate;
        ``task``/``heartbeat`` re-run their (idempotent-friendly) agent turn.
        """
        now = time.time() if now is None else now
        due: _List[Schedule] = []
        with self.db.transaction():
            rows = self.db.query(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=? "
                "AND (firing_until IS NULL OR firing_until<=?) ORDER BY next_run", (now, now))
            for r in rows:
                self.db.execute("UPDATE schedules SET firing_until=? WHERE id=?", (now + lease_s, r["id"]))
                due.append(_row(r))
        return due

    def complete_firing(self, sid: str, tz: str, now: float | None = None) -> None:
        """Advance a leased schedule after its firing was dispatched: compute next_run, clear the lease."""
        now = time.time() if now is None else now
        s = self.get(sid)
        if s is None:
            return
        nxt = next_run(s.kind, s.expr, tz, now)
        self.db.execute("UPDATE schedules SET last_run=?, run_count=run_count+1, next_run=?, enabled=?, "
                        "firing_until=NULL WHERE id=?", (now, nxt, 0 if nxt is None else 1, sid))

    def release_firing(self, sid: str) -> None:
        """Drop the lease without advancing, so the schedule is retried on the next tick (e.g. fire raised)."""
        self.db.execute("UPDATE schedules SET firing_until=NULL WHERE id=?", (sid,))
