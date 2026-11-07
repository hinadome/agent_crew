"""One tiny thread-safe SQLite wrapper shared by sessions, memory and tasks.

Strands calls session repositories synchronously (possibly from worker threads), so we use the
stdlib ``sqlite3`` behind a lock rather than an async driver. WAL mode keeps readers unblocked.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")

    def script(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def ensure_column(self, table: str, column: str, ddl: str) -> None:
        """Idempotent ``ALTER TABLE ... ADD COLUMN`` so existing databases upgrade in place."""
        with self._lock:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        """First column of the first row (for ``SELECT COUNT(*)``-style queries that always return a row)."""
        row = self.one(sql, params)
        if row is None:
            raise RuntimeError(f"query returned no row: {sql[:60]}")
        return row[0]

    def transaction(self) -> _Tx:
        return _Tx(self)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Tx:
    """``with db.transaction():`` -- atomic multi-statement block (lock is reentrant)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def __enter__(self) -> None:
        self._db._lock.acquire()
        self._db._conn.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        try:
            self._db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._db._lock.release()
