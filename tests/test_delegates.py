"""Delegation: stateful vs stateless specialists, serialisation, nesting limit, and the MCP client pool."""

import asyncio
import json
import time

import pytest

from kiro_crew.app import App
from kiro_crew.core.types import Address

from .conftest import make_config

FIRST = "first secret apple"


def _app(tmp_path, *, stateful, **over):
    agents = {"main": {"system_prompt": "MAIN", "delegates": ["helper"]},
              "helper": {"system_prompt": "HELPER", "description": "helps", "stateful": stateful}}
    return App(make_config(tmp_path, agents=over.pop("agents", agents), **over))


def calls_for(model, request):
    """Model calls made by the DELEGATE agent for ``request``.

    The delegate's newest message is the plain request. The parent's calls also mention it, but inside the
    ``[[tool:...]]`` marker, and the parent's history naturally contains earlier requests, so they must be excluded
    or every "does it remember?" assertion would pass vacuously.
    """
    def newest_text(msgs):
        return " ".join(b.get("text", "") for b in msgs[-1]["content"] if "text" in b)

    return [m for m in model.seen if request in newest_text(m) and "[[tool:" not in newest_text(m)]


async def ask(agent, request):
    await agent.invoke_async('[[tool:ask_helper:{"request": "' + request + '"}]]')


async def test_stateless_delegate_forgets_between_calls(tmp_path):
    app = _app(tmp_path, stateful=False)
    parent = app.factory.create("main", session_id="s1", user_id="u")
    await ask(parent, FIRST)
    await ask(parent, "second")
    second = calls_for(app.ctx.models.profile("a"), "second")
    assert second and FIRST not in json.dumps(second[0])  # a fresh agent each time
    assert app.ctx.storage.sessions.read_session("s1~helper") is None


async def test_stateful_delegate_remembers_earlier_calls(tmp_path):
    app = _app(tmp_path, stateful=True)
    parent = app.factory.create("main", session_id="s1", user_id="u")
    await ask(parent, FIRST)
    await ask(parent, "second")
    second = calls_for(app.ctx.models.profile("a"), "second")
    assert second and FIRST in json.dumps(second[0])  # it kept its own conversation
    assert app.ctx.storage.sessions.read_session("s1~helper") is not None


async def test_stateful_delegate_memory_survives_the_parent_being_rebuilt(tmp_path):
    app = _app(tmp_path, stateful=True)
    await ask(app.factory.create("main", session_id="s1", user_id="u"), FIRST)
    # e.g. the gateway evicted the parent from its cache, or the process restarted
    await ask(app.factory.create("main", session_id="s1", user_id="u"), "after rebuild")
    after = calls_for(app.ctx.models.profile("a"), "after rebuild")
    assert after and FIRST in json.dumps(after[0])  # restored from the delegate's persisted session


async def test_stateful_delegates_of_different_conversations_do_not_share_history(tmp_path):
    app = _app(tmp_path, stateful=True)
    await ask(app.factory.create("main", session_id="convA", user_id="u"), FIRST)
    await ask(app.factory.create("main", session_id="convB", user_id="u"), "other conversation")
    other = calls_for(app.ctx.models.profile("a"), "other conversation")
    assert other and FIRST not in json.dumps(other[0])


async def test_stateful_falls_back_to_stateless_when_the_parent_has_no_session(tmp_path):
    app = _app(tmp_path, stateful=True)
    parent = app.factory.create("main", session_id=None, user_id="u")  # ephemeral parent: nothing to scope to
    await ask(parent, FIRST)
    await ask(parent, "second")
    assert FIRST not in json.dumps(calls_for(app.ctx.models.profile("a"), "second")[0])


async def test_new_command_also_forgets_stateful_delegates(tmp_path):
    app = _app(tmp_path, stateful=True)
    await app.gateway.start()
    ch = app.gateway.channels["fake"]
    key = Address("fake", "c1").key
    await ch.say('[[tool:ask_helper:{"request": "' + FIRST + '"}]]')
    assert app.ctx.storage.sessions.read_session(f"{key}~helper") is not None
    await ch.say("/new")
    assert app.ctx.storage.sessions.read_session(f"{key}~helper") is None  # delegate history wiped with the chat
    await ch.say('[[tool:ask_helper:{"request": "fresh start"}]]')
    assert FIRST not in json.dumps(calls_for(app.ctx.models.profile("a"), "fresh start")[0])


async def test_calls_to_a_stateful_delegate_are_serialised_but_stateless_ones_run_in_parallel(tmp_path):
    for stateful, minimum, maximum in ((True, 0.55, 5.0), (False, 0.0, 0.5)):
        app = _app(tmp_path / str(stateful), stateful=stateful)
        parent = app.factory.create("main", session_id="s", user_id="u")
        tool = parent.tool_registry.registry["ask_helper"]
        app.ctx.models.profile("a").delay = 0.3
        t0 = time.perf_counter()
        results = await asyncio.gather(tool(request="x1"), tool(request="x2"))  # no "concurrent invocation" error
        took = time.perf_counter() - t0
        assert len(results) == 2 and minimum <= took < maximum, (stateful, took)


async def test_delegate_is_read_only_for_memory_even_when_stateful(tmp_path):
    app = _app(tmp_path, stateful=True)
    sub = app.factory.create("helper", session_id="s~helper", user_id="u", _depth=1)
    assert "search_memory" in sub.tool_names and "add_memory" not in sub.tool_names


async def test_nesting_is_capped_at_two_levels(tmp_path):
    agents = {"a": {"delegates": ["b"], "description": "a"}, "b": {"delegates": ["c"], "description": "b"},
              "c": {"delegates": ["d"], "description": "c"}, "d": {"description": "d"}}
    app = App(make_config(tmp_path, entry_agent="a", agents=agents))
    ask_tools = {depth: [t for t in app.factory.create(name, session_id=None, user_id="u", _depth=depth).tool_names
                         if t.startswith("ask_")] for depth, name in enumerate("abc")}
    assert ask_tools == {0: ["ask_b"], 1: ["ask_c"], 2: []}  # c may not delegate to d: recursion is bounded


# ---- MCP client pool (hermetic: a stub stands in for strands' MCPClient) ------------------------------
class StubClient:
    def __init__(self):
        self.consumers, self.stopped = set(), 0

    def add_consumer(self, c):
        self.consumers.add(c)

    def remove_consumer(self, c):
        self.consumers.discard(c)

    def stop(self, *a):
        self.stopped += 1


@pytest.fixture
def pooled(tmp_path, monkeypatch):
    app = App(make_config(tmp_path, mcp_servers=[{"name": "calc", "command": "x", "args": ["1"]},
                                                 {"name": "docs", "url": "http://h/mcp"}],
                          agents={"main": {"mcp_servers": ["calc", "docs"]}, "other": {"mcp_servers": ["calc"]}}))
    built = []
    monkeypatch.setattr(app.factory, "_build_mcp_client", lambda m: built.append(m.name) or StubClient())
    return app, built


def test_one_client_per_server_is_shared_by_every_agent(pooled):
    app, built = pooled
    for _ in range(3):
        app.factory.create("main", session_id=None, user_id="u")
    app.factory.create("other", session_id=None, user_id="u")
    assert sorted(built) == ["calc", "docs"]  # built once each, however many agents/calls use them
    client = next(iter(app.factory._mcp_pool.values()))
    assert "kiro-crew:mcp-pool" in client.consumers  # pinned: stays alive between short-lived agents


def test_close_stops_every_pooled_server(pooled):
    app, _ = pooled
    app.factory.create("main", session_id=None, user_id="u")
    clients = list(app.factory._mcp_pool.values())
    app.factory.close()
    assert all(c.stopped == 1 and not c.consumers for c in clients) and not app.factory._mcp_pool


def test_refresh_stops_only_servers_that_changed_or_disappeared(pooled):
    app, _ = pooled
    app.factory.create("main", session_id=None, user_id="u")
    by_name = {k[0]: c for k, c in app.factory._mcp_pool.items()}
    app.ctx.config.mcp_servers = [app.ctx.config.mcp_servers[0].model_copy(update={"args": ["2"]})]  # calc changed, docs gone
    app.factory.refresh_mcp()
    assert by_name["calc"].stopped == 1 and by_name["docs"].stopped == 1 and not app.factory._mcp_pool
    app.factory.create("main", session_id=None, user_id="u")  # config now has only calc
    assert len(app.factory._mcp_pool) == 1 and next(iter(app.factory._mcp_pool))[0] == "calc"
