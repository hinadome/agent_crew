"""Default storage backend: everything in one local SQLite file (WAL mode)."""

from __future__ import annotations

from ..core.config import Config
from ..memory.db import Database
from ..memory.session_repo import SQLiteSessionRepository
from ..memory.workspace import Workspace
from ..scheduler.store import ScheduleStore
from ..security.pairing import PairingStore
from ..tasks.store import TaskStore
from . import Storage, storage_backends

_SCHEMA = "CREATE TABLE IF NOT EXISTS conversation_settings (session_key TEXT PRIMARY KEY, model_profile TEXT)"


class SQLiteSettings:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(_SCHEMA)
        db.ensure_column("conversation_settings", "agent", "TEXT")  # added with per-conversation agents

    def get_model_profile(self, session_key: str) -> str | None:
        row = self.db.one("SELECT model_profile FROM conversation_settings WHERE session_key=?", (session_key,))
        return row["model_profile"] if row else None

    def set_model_profile(self, session_key: str, profile: str) -> None:
        self.db.execute("INSERT INTO conversation_settings(session_key, model_profile) VALUES (?,?) "
                        "ON CONFLICT(session_key) DO UPDATE SET model_profile=excluded.model_profile",
                        (session_key, profile))

    def get_agent(self, session_key: str) -> str | None:
        row = self.db.one("SELECT agent FROM conversation_settings WHERE session_key=?", (session_key,))
        return row["agent"] if row else None

    def set_agent(self, session_key: str, agent: str | None) -> None:
        self.db.execute("INSERT INTO conversation_settings(session_key, agent) VALUES (?,?) "
                        "ON CONFLICT(session_key) DO UPDATE SET agent=excluded.agent", (session_key, agent))


@storage_backends.register("sqlite")
def build_sqlite(cfg: Config, workspace: Workspace) -> Storage:
    db = Database(workspace.db_path)
    return Storage(
        sessions=SQLiteSessionRepository(db), tasks=TaskStore(db), schedules=ScheduleStore(db),
        pairing=PairingStore(db), settings=SQLiteSettings(db), db=db, persistent=True, _closers=[db.close])
