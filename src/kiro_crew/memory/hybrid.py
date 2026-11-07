"""Hybrid (semantic + keyword) long-term memory.

Facts are embedded when written. Search runs BM25 (FTS5) and cosine similarity over the user's vectors,
then merges both rankings with reciprocal-rank fusion, so exact names/IDs and paraphrases both work.

Scale note: vectors are scanned in memory per query (NumPy if installed, else pure Python). That is fine
for thousands of facts per user. For much larger stores register a real vector database backend.
If the embedding service is down, writes still succeed (run ``kiro-crew memory reindex`` later) and
searches fall back to keyword-only.
"""

from __future__ import annotations

import logging
from array import array
from typing import Any

from strands.memory import MemoryEntry, SearchOptions

from .db import Database
from .embeddings import Embedder, embedders
from .longterm import SQLiteMemoryStore, memory_backends

try:  # optional accelerator: `pip install kiro-crew[semantic]`
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
RRF_K = 60
CANDIDATES = 20
BATCH = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL);
"""


def _normalise(v: list[float]) -> array:
    a = array("f", v)
    norm = sum(x * x for x in a) ** 0.5 or 1.0
    return array("f", (x / norm for x in a))


def _scores(query: array, blobs: list[bytes], dim: int) -> list[float]:
    if np is not None:
        mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), dim)
        return (mat @ np.frombuffer(query.tobytes(), dtype=np.float32)).tolist()
    out = []
    for b in blobs:
        a = array("f")
        a.frombytes(b)
        out.append(sum(x * y for x, y in zip(a, query, strict=True)))
    return out


class SQLiteHybridMemoryStore(SQLiteMemoryStore):
    def __init__(self, db: Database, namespace: str, *, embedder: Embedder, min_similarity: float = 0.3,
                 **kwargs: Any) -> None:
        super().__init__(db, namespace, **kwargs)
        self.embedder = embedder
        self.min_similarity = min_similarity
        db.script(_SCHEMA)

    async def _store_vectors(self, items: list[tuple[int, str]]) -> None:
        vecs = await self.embedder.embed([t for _, t in items])
        for (mid, _), v in zip(items, vecs, strict=True):
            n = _normalise(v)
            self.db.execute("INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?,?)",
                            (mid, self.embedder.model, len(n), n.tobytes()))

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> bool:
        mid = self._insert(content)
        if mid is None:
            return False
        try:
            await self._store_vectors([(mid, content.strip())])
        except Exception as e:  # noqa: BLE001  (fact is saved; embedding can be backfilled by `reindex`)
            logger.warning("embedding failed for memory %s (%s); keyword search still works", mid, type(e).__name__)
        return True

    async def reindex(self) -> int:
        """Embed every fact in this namespace that lacks a vector for the configured model."""
        rows = self.db.query(
            "SELECT m.id, m.content FROM memories m LEFT JOIN memory_vectors v "
            "ON v.memory_id=m.id AND v.model=? WHERE m.namespace=? AND v.memory_id IS NULL",
            (self.embedder.model, self.namespace))
        items = [(r["id"], r["content"]) for r in rows]
        for i in range(0, len(items), BATCH):
            await self._store_vectors(items[i:i + BATCH])
        return len(items)

    async def _vector_rank(self, query: str) -> list[int]:
        rows = self.db.query(
            "SELECT v.memory_id, v.vec, v.dim FROM memory_vectors v JOIN memories m ON m.id=v.memory_id "
            "WHERE m.namespace=? AND v.model=?", (self.namespace, self.embedder.model))
        if not rows:
            return []
        q = _normalise((await self.embedder.embed([query]))[0])
        rows = [r for r in rows if r["dim"] == len(q)]
        if not rows:
            return []
        scores = _scores(q, [r["vec"] for r in rows], len(q))
        ranked = sorted(zip(scores, (r["memory_id"] for r in rows), strict=True), reverse=True)
        return [mid for sc, mid in ranked[:CANDIDATES] if sc >= self.min_similarity]

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = (options or {}).get("max_search_results") or self.max_search_results
        kw = self._keyword_rows(query, CANDIDATES)
        try:
            vec_ids = await self._vector_rank(query)
        except Exception as e:  # noqa: BLE001  (degrade to keyword search rather than fail the turn)
            logger.warning("semantic search unavailable (%s); using keyword search", type(e).__name__)
            vec_ids = []
        fused: dict[int, float] = {}
        for rank, r in enumerate(kw):
            fused[r["id"]] = fused.get(r["id"], 0) + 1 / (RRF_K + rank)
        for rank, mid in enumerate(vec_ids):
            fused[mid] = fused.get(mid, 0) + 1 / (RRF_K + rank)
        top = sorted(fused, key=lambda i: fused[i], reverse=True)[:limit]
        if not top:
            return []
        marks = ",".join("?" * len(top))
        by_id = {r["id"]: r for r in self.db.query(
            f"SELECT id, content, created_at FROM memories WHERE id IN ({marks})", top)}
        return [MemoryEntry(content=by_id[i]["content"], metadata={"id": i, "created_at": by_id[i]["created_at"],
                                                                    "score": fused[i]}) for i in top if i in by_id]


@memory_backends.register("hybrid")
def _hybrid_backend(storage: Any, namespace: str, **options: Any) -> SQLiteHybridMemoryStore:
    from ..storage import require_db

    db = require_db(storage, "memory.backend 'hybrid'")
    emb = dict(options.pop("embedder", None) or {})
    if not emb:
        raise ValueError("memory.backend 'hybrid' requires memory.options.embedder: {provider, model, ...}")
    provider = emb.pop("provider")
    return SQLiteHybridMemoryStore(db, namespace, embedder=embedders.get(provider)(**emb), **options)
