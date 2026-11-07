"""Non-persistent long-term memory (``memory.backend: inmem``), for the ``memory`` storage backend and tests.

Same behaviour as the SQLite keyword store (namespaced, idempotent writes, list/delete/clear), with simple
word-overlap ranking instead of FTS5/BM25. Facts live in the ``Storage`` object so every store instance for
a user sees the same data during the process lifetime.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from strands.memory import MemoryEntry, MemoryStore, SearchOptions

from .longterm import memory_backends

_WORD = re.compile(r"\w+", re.UNICODE)
_FALLBACK: dict[str, Any] = {}  # used if a backend is built without a Storage (e.g. a bare Database)


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if len(w) > 2}


class InMemoryFactStore(MemoryStore):
    def __init__(self, state: dict[str, Any], namespace: str, *, name: str = "long_term",
                 max_search_results: int = 5, writable: bool = True, extraction: Any = None) -> None:
        self._state = state
        self.namespace = namespace
        self.name = name
        self.description = "Durable facts, preferences and decisions about the user and their projects."
        self.max_search_results = max_search_results
        self.writable = writable
        self.extraction = extraction
        self._state.setdefault("lock", threading.RLock())
        self._state.setdefault("next_id", 1)
        self._state.setdefault("facts", {})  # namespace -> list[dict]

    def _facts(self) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = self._state["facts"].setdefault(self.namespace, [])
        return facts

    async def search(self, query: str, options: SearchOptions | None = None) -> list[MemoryEntry]:
        limit = (options or {}).get("max_search_results") or self.max_search_results
        q = _words(query)
        if not q:
            return []
        with self._state["lock"]:
            scored = sorted(((len(q & _words(f["content"])), f) for f in self._facts()),
                            key=lambda t: (-t[0], -t[1]["id"]))
            return [MemoryEntry(content=f["content"], metadata={"id": f["id"], "created_at": f["created_at"]})
                    for score, f in scored if score > 0][:limit]

    def _insert(self, content: str) -> int | None:
        content = content.strip()
        if not content:
            return None
        with self._state["lock"]:
            if any(f["content"] == content for f in self._facts()):
                return None  # idempotent, like UNIQUE(namespace, content)
            fid = self._state["next_id"]
            self._state["next_id"] = fid + 1
            self._facts().append({"id": fid, "content": content, "created_at": time.time()})
            return int(fid)

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> bool:
        return self._insert(content) is not None

    def list(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._state["lock"]:
            items = sorted(self._facts(), key=lambda f: -f["id"])[offset:offset + limit]
            return [dict(f) for f in items]

    def count(self) -> int:
        with self._state["lock"]:
            return len(self._facts())

    def delete(self, memory_id: int) -> bool:
        with self._state["lock"]:
            facts = self._facts()
            for i, f in enumerate(facts):
                if f["id"] == memory_id:
                    del facts[i]
                    return True
            return False

    def clear(self) -> int:
        with self._state["lock"]:
            n = len(self._facts())
            self._facts().clear()
            return n


@memory_backends.register("inmem")
def _inmem_backend(storage: Any, namespace: str, **options: Any) -> InMemoryFactStore:
    extras = getattr(storage, "extras", None)
    state = extras.setdefault("facts_state", {}) if isinstance(extras, dict) else _FALLBACK
    return InMemoryFactStore(state, namespace, **options)
