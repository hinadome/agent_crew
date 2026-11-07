import sqlite3
import time

import pytest

from kiro_crew.agents.factory import AgentFactory
from kiro_crew.memory.db import Database
from kiro_crew.memory.embeddings import Embedder, embedders
from kiro_crew.memory.hybrid import SQLiteHybridMemoryStore
from kiro_crew.memory.longterm import SQLiteMemoryStore
from kiro_crew.memory.maintenance import prune_sessions, run_once
from kiro_crew.memory.session_repo import SQLiteSessionRepository
from kiro_crew.memory.workspace import MAX_CORE_CHARS, Workspace


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


# ---- keyword store -------------------------------------------------------------------------------
async def test_add_search_dedupe(db):
    s = SQLiteMemoryStore(db, "u")
    assert await s.add("Alice prefers dark mode; deploys on Fridays")
    assert not await s.add("Alice prefers dark mode; deploys on Fridays")  # idempotent
    assert not await s.add("   ")
    hits = await s.search("which mode does she prefer?")
    assert hits and hits[0].content.startswith("Alice")


@pytest.mark.parametrize("q", ['"; DROP TABLE memories; --', "a OR b NOT c", "NEAR(x y)", "*", "   ", "ab", "'", "x:y"])
async def test_search_is_injection_safe(db, q):
    s = SQLiteMemoryStore(db, "u")
    await s.add("harmless fact about bananas")
    await s.search(q)  # must not raise
    assert s.count() == 1


async def test_namespace_isolation(db):
    a, b = SQLiteMemoryStore(db, "user:a"), SQLiteMemoryStore(db, "user:b")
    await a.add("a likes tea")
    assert await b.search("tea") == [] and b.count() == 0
    assert not b.delete(a.list()[0]["id"])  # cannot delete another namespace's fact
    assert b.clear() == 0 and a.count() == 1


async def test_list_delete_clear(db):
    s = SQLiteMemoryStore(db, "u")
    for t in ("one fact", "two fact", "three fact"):
        await s.add(t)
    items = s.list()
    assert [i["content"] for i in items] == ["three fact", "two fact", "one fact"]  # newest first
    assert s.delete(items[0]["id"]) and s.count() == 2
    assert await s.search("three") == []  # removed from the search index too
    assert s.clear() == 2 and s.count() == 0


# ---- hybrid / semantic ---------------------------------------------------------------------------
SYN = {"cat": 0, "cats": 0, "feline": 0, "kitten": 0, "dog": 1, "dogs": 1, "canine": 1, "tea": 2}


class FakeEmbedder(Embedder):
    """Maps synonyms onto the same axis so we can prove retrieval works with zero word overlap."""

    calls = 0
    broken = False

    async def embed(self, texts):
        FakeEmbedder.calls += 1
        if FakeEmbedder.broken:
            raise ConnectionError("embedding service down")
        out = []
        for t in texts:
            v = [0.0] * 8
            for w in t.lower().replace(".", " ").split():
                if w in SYN:
                    v[SYN[w]] += 1
            out.append(v)
        return out


embedders.add("fake", FakeEmbedder)


@pytest.fixture
def hybrid(db):
    FakeEmbedder.broken, FakeEmbedder.calls = False, 0
    return SQLiteHybridMemoryStore(db, "u", embedder=FakeEmbedder(model="fake-1"))


async def test_semantic_match_without_shared_words(hybrid):
    await hybrid.add("I adore cats")
    await hybrid.add("I own a dog")
    await hybrid.add("Meeting notes about budget")
    hits = await hybrid.search("feline")  # no keyword overlap with any stored fact
    assert [h.content for h in hits] == ["I adore cats"]


async def test_keyword_still_works_in_hybrid(hybrid):
    await hybrid.add("Project codename is Falcon-77")
    assert (await hybrid.search("Falcon-77"))[0].content.startswith("Project")


async def test_embedding_outage_degrades_gracefully_and_reindex_heals(hybrid):
    FakeEmbedder.broken = True
    assert await hybrid.add("kitten photos folder")  # fact is saved even though embedding failed
    assert (await hybrid.search("kitten"))[0].content == "kitten photos folder"  # keyword fallback
    FakeEmbedder.broken = False
    assert await hybrid.search("feline") == []  # no vector yet
    assert await hybrid.reindex() == 1
    assert [h.content for h in await hybrid.search("feline")] == ["kitten photos folder"]
    assert await hybrid.reindex() == 0  # idempotent


async def test_hybrid_delete_removes_vectors_and_namespace_isolated(db):
    a = SQLiteHybridMemoryStore(db, "a", embedder=FakeEmbedder(model="m"))
    b = SQLiteHybridMemoryStore(db, "b", embedder=FakeEmbedder(model="m"))
    await a.add("I love cats")
    assert await b.search("feline") == []
    a.delete(a.list()[0]["id"])
    assert db.one("SELECT COUNT(*) AS n FROM memory_vectors")["n"] == 0  # ON DELETE CASCADE


async def test_model_change_ignores_old_vectors(db):
    old = SQLiteHybridMemoryStore(db, "u", embedder=FakeEmbedder(model="v1"))
    await old.add("I love cats")
    new = SQLiteHybridMemoryStore(db, "u", embedder=FakeEmbedder(model="v2"))
    assert await new.search("feline") == []  # v1 vectors are not comparable to v2
    assert await new.reindex() == 1
    assert len(await new.search("feline")) == 1


def test_hybrid_backend_requires_embedder(db):
    import kiro_crew.memory.factory  # noqa: F401
    from kiro_crew.memory.longterm import memory_backends

    with pytest.raises(ValueError, match="embedder"):
        memory_backends.get("hybrid")(db, "u")
    store = memory_backends.get("hybrid")(db, "u", embedder={"provider": "fake", "model": "m"})
    assert isinstance(store, SQLiteHybridMemoryStore)


async def test_openai_embedder_wire_format(monkeypatch):
    import json

    import httpx

    from kiro_crew.memory import embeddings

    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers.get("authorization"), json.loads(req.content)
        # return out of order to prove we sort by index
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})

    monkeypatch.setattr(embeddings, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    e = embeddings.OpenAIEmbedder(model="m", api_key="k", base_url="http://emb.test/v1")
    assert await e.embed(["a", "b"]) == [[1, 0], [0, 1]]
    assert seen["url"] == "http://emb.test/v1/embeddings" and seen["auth"] == "Bearer k"
    assert seen["body"] == {"model": "m", "input": ["a", "b"]}


# ---- sessions / workspace / migration ------------------------------------------------------------
async def test_session_repo_roundtrip_through_agent(app):
    ag = app.factory.create(None, session_id="s-1", user_id="u")
    await ag.invoke_async("remember the word kumquat")
    ag2 = app.factory.create(None, session_id="s-1", user_id="u")
    assert "kumquat" in str(ag2.messages)
    app.factory.reset_session("s-1")
    assert app.factory.create(None, session_id="s-1", user_id="u").messages == []


async def test_prune_sessions_by_age_but_protects_live_tasks(app):
    repo = app.ctx.storage.sessions
    for sid in ("old-chat", "fresh-chat", "task-live", "task-dead"):
        await app.factory.create(None, session_id=sid, user_id="u").invoke_async("hi")
    live = app.ctx.tasks.create("live", "g", agent=None, address=None, user_id="u")
    app.ctx.db.execute("UPDATE tasks SET id='live', status='running' WHERE id=?", (live.id,))
    old = time.time() - 200 * 86400
    for sid in ("old-chat", "task-live", "task-dead"):
        app.ctx.db.execute("UPDATE session_messages SET updated_at=? WHERE session_id=?", (old, sid))
    assert prune_sessions(repo, 0) == 0  # 0 means keep forever
    assert prune_sessions(repo, 90, app.ctx.tasks.active_session_ids()) == 2  # old-chat, task-dead
    left = {r["session_id"] for r in app.ctx.db.query("SELECT session_id FROM sessions")}
    assert left == {"fresh-chat", "task-live"}


def test_housekeeping_purges_finished_tasks(app):
    t = app.ctx.tasks.create("done", "g", agent=None, address=None, user_id="u")
    app.ctx.tasks.finish(t.id, "succeeded", result="x")
    app.ctx.db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (time.time() - 40 * 86400, t.id))
    keep = app.ctx.tasks.create("queued", "g", agent=None, address=None, user_id="u")
    res = run_once(app.ctx)
    assert res["tasks"] == 1 and app.ctx.tasks.get(keep.id) is not None and app.ctx.tasks.get(t.id) is None


def test_old_database_is_upgraded_in_place(tmp_path):
    """A v0.1 database (no tasks.quiet_token / session_messages.updated_at) must keep working."""
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL,
        agent TEXT, channel TEXT, conversation_id TEXT, thread_id TEXT, user_id TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, progress TEXT NOT NULL DEFAULT '[]', result TEXT, error TEXT,
        not_before REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
    INSERT INTO tasks(id,title,goal,status,created_at,updated_at) VALUES ('t1','T','G','queued',1,1);
    CREATE TABLE sessions (session_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL DEFAULT 0);
    CREATE TABLE session_messages (session_id TEXT NOT NULL, agent_id TEXT NOT NULL, message_id INTEGER NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY (session_id, agent_id, message_id));
    INSERT INTO session_messages VALUES ('s','a',0,'{}');
    """)
    c.commit()
    c.close()
    from kiro_crew.tasks.store import TaskStore

    d = Database(path)
    store = TaskStore(d)
    SQLiteSessionRepository(d)
    SQLiteSessionRepository(d)  # idempotent
    assert store.get("t1").quiet_token is None
    assert store.create("new", "g", agent=None, address=None, user_id="u", quiet_token="X").quiet_token == "X"
    assert d.one("SELECT updated_at FROM session_messages")["updated_at"] > 0  # legacy rows not pruned instantly
    d.close()


def test_workspace_files_and_context_budget(tmp_path):
    ws = Workspace(tmp_path / "w")
    assert (tmp_path / "w" / "SOUL.md").exists() and ws.files_dir.is_dir()
    ws.append_core_memory("likes tea")
    assert "likes tea" in ws.system_context()
    ws.memory_path.write_text("old " * 5000 + "NEWEST")
    ctx = ws.system_context()
    assert "NEWEST" in ctx and "truncated" in ctx and len(ctx) < 2 * MAX_CORE_CHARS


def test_memory_injection_and_tools_wired(app):
    factory: AgentFactory = app.factory
    top = factory.create(None, session_id="s", user_id="u")
    assert {"search_memory", "add_memory"} <= set(top.tool_names)
    sub = factory.create("helper", session_id=None, user_id="u")
    assert "search_memory" in sub.tool_names and "add_memory" not in sub.tool_names  # delegates read-only


async def test_stored_fact_is_injected_into_prompt(app, chan, models):
    from kiro_crew.memory.factory import make_store

    await make_store(app.ctx, "alice").add("Alice prefers dark mode")
    await chan.say("which mode do I prefer? dark?")
    assert "Alice prefers dark mode" in models["A"].prompt_text()
