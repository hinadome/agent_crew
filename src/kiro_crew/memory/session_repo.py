"""Durable conversation history (the "episodic / short-term" layer) in SQLite.

Implements Strands' ``SessionRepository`` so it plugs into ``RepositorySessionManager``.
Swap for S3 / DynamoDB / Postgres by providing another ``SessionRepository``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Collection
from typing import Any

from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from ..storage import SessionStore
from .db import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL DEFAULT (unixepoch('subsec')));
CREATE TABLE IF NOT EXISTS session_agents (
    session_id TEXT NOT NULL, agent_id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id));
CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL, agent_id TEXT NOT NULL, message_id INTEGER NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id, message_id));
CREATE TABLE IF NOT EXISTS session_multi_agents (
    session_id TEXT NOT NULL, multi_agent_id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (session_id, multi_agent_id));
"""


class SQLiteSessionRepository(SessionStore):
    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(_SCHEMA)
        # added after v0.1 (needed for retention): upgrade older databases in place
        db.ensure_column("session_messages", "updated_at", "REAL")
        db.execute("UPDATE session_messages SET updated_at=? WHERE updated_at IS NULL", (time.time(),))

    # -- sessions
    def create_session(self, session: Session, **kwargs: Any) -> Session:
        if self.db.one("SELECT 1 FROM sessions WHERE session_id=?", (session.session_id,)):
            raise SessionException(f"Session {session.session_id} already exists")
        self.db.execute("INSERT INTO sessions(session_id, data) VALUES (?,?)",
                        (session.session_id, json.dumps(session.to_dict())))
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        row = self.db.one("SELECT data FROM sessions WHERE session_id=?", (session_id,))
        return Session.from_dict(json.loads(row["data"])) if row else None

    def delete_session(self, session_id: str) -> None:
        with self.db.transaction():
            for t in ("session_messages", "session_agents", "session_multi_agents", "sessions"):
                self.db.execute(f"DELETE FROM {t} WHERE session_id=?", (session_id,))

    def delete_prefix(self, prefix: str) -> int:
        # substr() compares literally; LIKE would treat '_' and '%' (common in session keys) as wildcards.
        rows = self.db.query("SELECT session_id FROM sessions WHERE substr(session_id, 1, ?) = ?",
                             (len(prefix), prefix))
        for r in rows:
            self.delete_session(r["session_id"])
        return len(rows)

    def prune_idle(self, older_than_s: float, protected: Collection[str] = ()) -> int:
        cutoff = time.time() - older_than_s
        rows = self.db.query(
            "SELECT s.session_id FROM sessions s LEFT JOIN "
            "(SELECT session_id, MAX(updated_at) AS m FROM session_messages GROUP BY session_id) x "
            "ON x.session_id = s.session_id WHERE COALESCE(x.m, s.updated_at) < ?", (cutoff,))
        doomed = [r["session_id"] for r in rows if r["session_id"] not in set(protected)]
        for sid in doomed:
            self.delete_session(sid)
        return len(doomed)

    # -- agents
    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO session_agents VALUES (?,?,?)",
                        (session_id, session_agent.agent_id, json.dumps(session_agent.to_dict())))

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        row = self.db.one("SELECT data FROM session_agents WHERE session_id=? AND agent_id=?",
                          (session_id, agent_id))
        return SessionAgent.from_dict(json.loads(row["data"])) if row else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        prev = self.read_agent(session_id, session_agent.agent_id)
        if prev is None:
            raise SessionException(f"Agent {session_agent.agent_id} in session {session_id} does not exist")
        session_agent.created_at = prev.created_at
        self.create_agent(session_id, session_agent)

    # -- messages
    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO session_messages(session_id, agent_id, message_id, data, updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (session_id, agent_id, session_message.message_id, json.dumps(session_message.to_dict()),
                         time.time()))

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any) -> SessionMessage | None:
        row = self.db.one(
            "SELECT data FROM session_messages WHERE session_id=? AND agent_id=? AND message_id=?",
            (session_id, agent_id, message_id))
        return SessionMessage.from_dict(json.loads(row["data"])) if row else None

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        prev = self.read_message(session_id, agent_id, session_message.message_id)
        if prev is None:
            raise SessionException(f"Message {session_message.message_id} does not exist")
        session_message.created_at = prev.created_at
        self.create_message(session_id, agent_id, session_message)

    def list_messages(self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0,
                      **kwargs: Any) -> list[SessionMessage]:
        rows = self.db.query(
            "SELECT data FROM session_messages WHERE session_id=? AND agent_id=? "
            "ORDER BY message_id LIMIT ? OFFSET ?",
            (session_id, agent_id, -1 if limit is None else limit, offset))
        return [SessionMessage.from_dict(json.loads(r["data"])) for r in rows]

    # -- multi-agent
    def create_multi_agent(self, session_id: str, multi_agent: Any, **kwargs: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO session_multi_agents VALUES (?,?,?)",
                        (session_id, multi_agent.id, json.dumps(multi_agent.serialize_state())))

    def read_multi_agent(self, session_id: str, multi_agent_id: str, **kwargs: Any) -> dict[str, Any] | None:
        row = self.db.one("SELECT data FROM session_multi_agents WHERE session_id=? AND multi_agent_id=?",
                          (session_id, multi_agent_id))
        return json.loads(row["data"]) if row else None

    def update_multi_agent(self, session_id: str, multi_agent: Any, **kwargs: Any) -> None:
        if self.read_multi_agent(session_id, multi_agent.id) is None:
            raise SessionException(f"MultiAgent state {multi_agent.id} in session {session_id} does not exist")
        self.create_multi_agent(session_id, multi_agent)
