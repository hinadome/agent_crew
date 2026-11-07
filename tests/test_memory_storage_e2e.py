"""The whole application running on the non-persistent ``memory`` storage backend: no SQLite involved.

Proves the storage abstraction is real: gateway, agents, tasks, scheduler, pairing and memory commands all work
through the protocols alone.
"""

import logging
import time

import pytest

from kiro_crew.app import App
from kiro_crew.core.types import Address
from kiro_crew.memory.factory import make_store

from .conftest import make_config, wait_for


@pytest.fixture
async def mapp(tmp_path):
    cfg = make_config(tmp_path, storage={"backend": "memory"}, memory={"backend": "inmem", "auto_extract": False},
                      channels={"fake": {"enabled": True, "allow_from": ["u1"], "access": "pairing"}},
                      admins=["fake:u1"])
    a = App(cfg)
    await a.gateway.start()
    a.runner.start()
    yield a
    await a.runner.stop()
    await a.gateway.stop()
    a.ctx.storage.close()


def test_no_sql_database_exists(mapp):
    assert mapp.ctx.storage.db is None and mapp.ctx.storage.persistent is False
    with pytest.raises(ValueError, match="no SQL database"):
        _ = mapp.ctx.db
    assert not mapp.ctx.workspace.db_path.exists()  # nothing was written to disk


async def test_chat_history_survives_agent_eviction(mapp):
    ch = mapp.gateway.channels["fake"]
    await ch.say("remember the word kumquat")
    mapp.gateway._agents.clear()  # force a rebuild from the stored session
    model = mapp.ctx.models.profile("a")
    model.seen.clear()
    await ch.say("what word?")
    assert "kumquat" in model.prompt_text()


async def test_new_and_model_switch_use_storage(mapp):
    ch = mapp.gateway.channels["fake"]
    await ch.say("hello")
    assert "Switched" in await ch.say("/model b")
    assert mapp.ctx.settings.get_model_profile(Address("fake", "c1").key) == "b"
    assert "[B]" in await ch.say("hi")
    assert "fresh" in await ch.say("/new")


async def test_pairing_flow_on_memory_storage(mapp):
    ch = mapp.gateway.channels["fake"]
    first = await ch.say("hi", user="newbie")
    code = mapp.ctx.pairing.pending()[0].code
    assert code in first
    assert "Approved fake:newbie" in await ch.say(f"/pair approve {code}", user="u1")
    assert "[A] ok" in await ch.say("hello", user="newbie")


async def test_background_task_runs_and_notifies(mapp):
    ch = mapp.gateway.channels["fake"]
    await ch.say('[[tool:start_task:{"title": "Big job", "goal": "do it"}]]')
    await wait_for(lambda: any("done: Big job" in m for m in ch.out))
    assert mapp.ctx.tasks.list()[0].status == "succeeded"


async def test_crash_recovery_on_memory_storage(mapp):
    t = mapp.ctx.tasks.create("Recover", "g", agent=None, address=Address("fake", "c1"), user_id="u1")
    mapp.ctx.tasks.claim_next()  # now 'running'; simulate a restart finding it that way
    # the worker loop may already have claimed it; either way recovery + completion must converge
    mapp.ctx.tasks.recover_interrupted()
    mapp.runner.wake()
    await wait_for(lambda: mapp.ctx.tasks.get(t.id).status == "succeeded")


async def test_scheduler_delivers_reminder(mapp):
    ch = mapp.gateway.channels["fake"]
    mapp.ctx.schedules.create(kind="at", expr=str(time.time() - 1), first_run=time.time() - 1, mode="message",
                              title="t", payload="stand up", agent=None, address=Address("fake", "c1"), user_id="u1")
    mapp.scheduler.start()
    try:
        await wait_for(lambda: any("Reminder: stand up" in m for m in ch.out), timeout=5)
    finally:
        await mapp.scheduler.stop()


async def test_memory_commands_and_injection(mapp):
    ch = mapp.gateway.channels["fake"]
    await make_store(mapp.ctx, "alice").add("Alice prefers dark mode")  # u1 is linked to "alice"
    assert "dark mode" in await ch.say("/memory")
    model = mapp.ctx.models.profile("a")
    await ch.say("which mode do I prefer? dark?")
    assert "Alice prefers dark mode" in model.prompt_text()  # injected via the in-process fact store
    fid = make_store(mapp.ctx, "alice").list()[0]["id"]
    assert "Forgotten" in await ch.say(f"/memory forget {fid}")


async def test_housekeeping_works_without_sql(mapp):
    from kiro_crew.memory.maintenance import run_once

    await mapp.gateway.channels["fake"].say("hello")
    assert run_once(mapp.ctx) == {"tasks": 0, "sessions": 0}  # nothing old; runs through the protocols


async def test_nothing_persists_across_apps(tmp_path):
    cfg = lambda: make_config(tmp_path, storage={"backend": "memory"}, memory={"backend": "inmem", "auto_extract": False})
    a = App(cfg())
    await a.gateway.start()
    await a.gateway.channels["fake"].say("secret word zebra")
    b = App(cfg())
    await b.gateway.start()
    model = b.ctx.models.profile("a")
    model.seen.clear()
    await b.gateway.channels["fake"].say("what word?")
    assert "zebra" not in model.prompt_text()  # a fresh process starts empty (by design)


def test_sql_only_memory_backends_fail_with_a_clear_message(tmp_path):
    cfg = make_config(tmp_path, storage={"backend": "memory"}, memory={"backend": "hybrid", "auto_extract": False,
                                                                       "options": {"embedder": {"provider": "openai"}}})
    app = App(cfg)
    with pytest.raises(ValueError, match="needs the 'sqlite' storage backend"):
        make_store(app.ctx, "u")
    cfg2 = make_config(tmp_path, storage={"backend": "memory"}, memory={"backend": "sqlite", "auto_extract": False})
    with pytest.raises(ValueError, match="needs the 'sqlite' storage backend"):
        make_store(App(cfg2).ctx, "u")


def test_unknown_storage_backend_is_a_clear_error(tmp_path):
    with pytest.raises(KeyError, match="Available"):
        App(make_config(tmp_path, storage={"backend": "postgres"}))


def test_memory_backend_warns_loudly(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        App(make_config(tmp_path, storage={"backend": "memory"}))
    assert any("NOT persisted" in r.message for r in caplog.records)
