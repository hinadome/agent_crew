"""Long-term semantic memory: durable facts/preferences/decisions that outlive any conversation.

``SQLiteMemoryStore`` implements Strands' ``MemoryStore`` protocol, so the stock ``MemoryManager``
handles retrieval-injection, the ``search_memory``/``add_memory`` tools, and background extraction.

Retrieval is FTS5 BM25 keyword search: zero extra dependencies and good enough to start. To go
semantic, register another backend (pgvector, Bedrock KB, mem0, ...) under ``memory_backends`` --
the rest of the app only sees the ``MemoryStore`` protocol.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from strands.memory import MemoryEntry, MemoryStore, SearchOptions

from ..core.registry import Registry
from .db import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY, namespace TEXT NOT NULL, content TEXT NOT NULL,
    metadata TEXT, created_at REAL NOT NULL, UNIQUE(namespace, content));
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content); END;
"""
_WORD = re.compile(r"\w+", re.UNICODE)


def _fts_query(text: str) -> str | None:
    """Turn free text into a safe OR-query ("a" OR "b"); quoting neutralises FTS operators."""
    words = {w.lower() for w in _WORD.findall(text) if len(w) > 2}
    return " OR ".join(f'"{w}"' for w in sorted(words)[:24]) or None


class SQLiteMemoryStore(MemoryStore):
    """Facts scoped by ``namespace`` (e.g. one per user), so memory never leaks across people."""

    def __init__(self, db: Database, namespace: str, *, name: str = "long_term", max_search_results: int = 5,
                 writable: bool = True, extraction: Any = None) -> None:
        self.db = db
        self.namespace = namespace
        self.name = name
        self.description = "Durable facts, preferences and decisions about the user and their projects."
        self.max_search_results = max_search_results
        self.writable = writable
        self.extraction = extraction
        db.script(_SCHEMA)

    # -- retrieval ---------------------------------------------------------------------------
    def _keyword_rows(self, query: str, limit: int) -> list[Any]:
        q = _fts_query(query)
        if not q:
            return []
        return self.db.query(
            "SELECT m.id, m.content, m.created_at, bm25(memories_fts) AS score FROM memories_fts "
            "JOIN memories m ON m.id = memories_fts.rowid "
            "WHERE memories_fts MATCH ? AND m.namespace = ? ORDER BY score LIMIT ?",
            (q, self.namespace, limit))

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = (options or {}).get("max_search_results") or self.max_search_results
        return [MemoryEntry(content=r["content"], metadata={"id": r["id"], "created_at": r["created_at"]})
                for r in self._keyword_rows(query, limit)]

    # -- writes ------------------------------------------------------------------------------
    def _insert(self, content: str) -> int | None:
        """Insert a fact; returns its id, or ``None`` if empty or already present (idempotent)."""
        content = content.strip()
        if not content:
            return None
        cur = self.db.execute(  # UNIQUE + OR IGNORE makes extraction's at-least-once writes idempotent
            "INSERT OR IGNORE INTO memories(namespace, content, metadata, created_at) VALUES (?,?,?,?)",
            (self.namespace, content, None, time.time()))
        return cur.lastrowid if cur.rowcount > 0 else None

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> bool:
        return self._insert(content) is not None

    # -- administration (used by /memory and `kiro-crew memory`) ------------------------------
    def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id, content, created_at FROM memories WHERE namespace=? "
                             "ORDER BY id DESC LIMIT ? OFFSET ?", (self.namespace, limit, offset))
        return [dict(r) for r in rows]

    def count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM memories WHERE namespace=?", (self.namespace,)))

    def delete(self, memory_id: int) -> bool:
        return self.db.execute("DELETE FROM memories WHERE id=? AND namespace=?",
                               (memory_id, self.namespace)).rowcount > 0

    def clear(self) -> int:
        return self.db.execute("DELETE FROM memories WHERE namespace=?", (self.namespace,)).rowcount


MemoryBackend = Callable[..., MemoryStore]  # (db, namespace, **options) -> MemoryStore
memory_backends: Registry[MemoryBackend] = Registry("memory_backend")


@memory_backends.register("sqlite")
def _sqlite_backend(storage: Any, namespace: str, **options: Any) -> MemoryStore:
    from ..storage import require_db

    return SQLiteMemoryStore(require_db(storage, "memory.backend 'sqlite'"), namespace, **options)
