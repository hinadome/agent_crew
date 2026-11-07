"""Hot reload: add/change/remove agents while the bot runs, and a bad edit must never take it down."""

import asyncio
import os
import signal
import textwrap

import pytest

from kiro_crew.agents.reload import _looks_like, signal_running_process
from kiro_crew.app import load_app
from kiro_crew.cli import main

from .conftest import wait_for

BASE = textwrap.dedent("""
    workspace: {ws}
    entry_agent: main
    models:
      default: a
      profiles:
        a: {{provider: fake, model_id: A}}
        b: {{provider: fake, model_id: B}}
    memory: {{auto_extract: false}}
    housekeeping: {{enabled: false}}
    agent_files: {{watch: {watch}, interval_s: 0.2}}
    admins: ["fake:u1"]
    agents:
      main:
        system_prompt: I AM MAIN
      fixed:
        system_prompt: I AM FIXED
        selectable: true
    channels:
      fake: {{enabled: true, allow_from: [u1, u2]}}
      fake2: {{enabled: true, allow_from: [u1], agent: fixed}}
""")


def write_cfg(tmp_path, watch="false", extra=""):
    p = tmp_path / "kiro-crew.yaml"
    p.write_text(BASE.format(ws=tmp_path / "ws", watch=watch) + extra)
    (tmp_path / "ws" / "agents").mkdir(parents=True, exist_ok=True)
    return p


def drop(tmp_path, name, text):
    (tmp_path / "ws" / "agents" / f"{name}.md").write_text(textwrap.dedent(text).lstrip())


def last_agent(model):
    t = repr(model.systems[-1])
    return next((n for n in ("MAIN", "FIXED", "FAQ", "NEW") if f"I AM {n}" in t), None)


@pytest.fixture
async def live(tmp_path):
    app = load_app(write_cfg(tmp_path))
    await app.gateway.start()
    yield app, tmp_path
    app.factory.close()
    app.ctx.storage.close()


# ---- adding / changing / removing -------------------------------------------------------------------
async def test_a_dropped_in_agent_is_usable_after_reload_without_restart(live):
    app, tmp = live
    ch = app.gateway.channels["fake"]
    assert "can't switch" in await ch.say("/agent faq", user="u1" if False else "u2")
    drop(tmp, "faq", "---\nselectable: true\ndescription: FAQ bot\n---\nI AM FAQ\n")
    res = app.reloader.reload()
    assert res.ok and res.added == ["faq"] and not res.removed and "added: faq" in res.render()
    assert "Switched" in await ch.say("/agent faq", user="u2")
    await ch.say("hi", user="u2")
    assert last_agent(app.ctx.models.profile("a")) == "FAQ"


async def test_changed_prompt_applies_to_the_next_message_and_keeps_history(live):
    app, tmp = live
    ch, model = app.gateway.channels["fake"], app.ctx.models.profile("a")
    drop(tmp, "faq", "---\nselectable: true\n---\nI AM FAQ\n")
    app.reloader.reload()
    await ch.say("/agent faq", user="u2")
    await ch.say("remember the word kumquat", user="u2")
    drop(tmp, "faq", "---\nselectable: true\n---\nI AM NEW\n")
    res = app.reloader.reload()
    assert res.changed == ["faq"]
    model.seen.clear()
    await ch.say("what word?", user="u2")
    assert last_agent(model) == "NEW"  # the cached agent was dropped, the new prompt is in use
    assert "kumquat" in model.prompt_text()  # the conversation's history for this agent survived the reload


async def test_removing_a_file_removes_the_agent_and_a_stale_selection_falls_back(live):
    app, tmp = live
    ch, model = app.gateway.channels["fake"], app.ctx.models.profile("a")
    drop(tmp, "faq", "---\nselectable: true\n---\nI AM FAQ\n")
    app.reloader.reload()
    await ch.say("/agent faq", user="u2")
    (tmp / "ws" / "agents" / "faq.md").unlink()
    res = app.reloader.reload()
    assert res.removed == ["faq"]
    await ch.say("still there?", user="u2")
    assert last_agent(model) == "MAIN"  # the saved choice points at a deleted agent -> default routing, no crash


async def test_no_op_reload_is_reported_as_such(live):
    app, _ = live
    res = app.reloader.reload()
    assert res.ok and res.noop and res.render() == "Reloaded. No agent changes."


async def test_parked_underscore_files_are_ignored(live):
    app, tmp = live
    drop(tmp, "_draft", "---\nselectable: true\n---\nI AM FAQ\n")
    assert app.reloader.reload().noop and "_draft" not in app.cfg.agents


# ---- a bad edit never breaks the running bot --------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "---\nmodel: no-such-profile\n---\nI AM FAQ\n",          # unknown reference
    "---\ndescription: [unclosed\n---\nI AM FAQ\n",           # broken YAML
    "---\ndescription: never closed\nI AM FAQ\n",             # unterminated front matter
    "---\nnonsense_field: 1\n---\nI AM FAQ\n",                # unknown setting
    "---\ndelegate_of: [ghost]\n---\nI AM FAQ\n",             # dangling parent
])
async def test_invalid_edits_are_rejected_and_the_old_agents_keep_working(live, bad):
    app, tmp = live
    ch, model = app.gateway.channels["fake"], app.ctx.models.profile("a")
    drop(tmp, "faq", bad)
    before = dict(app.cfg.agents)
    res = app.reloader.reload()
    assert not res.ok and "FAILED" in res.render() and "previous agents" in res.render()
    assert app.cfg.agents == before and "faq" not in app.cfg.agents
    assert "[A] ok" in await ch.say("still alive?", user="u2") and last_agent(model) == "MAIN"


async def test_breaking_the_yaml_config_itself_is_also_rejected(live):
    app, tmp = live
    (tmp / "kiro-crew.yaml").write_text("agents: [this is not valid for the schema")
    assert not app.reloader.reload().ok
    assert "[A] ok" in await app.gateway.channels["fake"].say("alive", user="u2")


async def test_reload_is_refused_if_it_would_remove_an_agent_a_running_channel_uses(live):
    app, tmp = live
    p = tmp / "kiro-crew.yaml"
    text = p.read_text()
    block = "  fixed:\n    system_prompt: I AM FIXED\n    selectable: true\n"
    assert block in text  # guard: the replacement below must really remove the agent
    p.write_text(text.replace(block, "").replace("agent: fixed", "agent: main"))
    res = app.reloader.reload()
    assert not res.ok and "channels.fake2.agent" in (res.error or "") and "fixed" in app.cfg.agents


async def test_non_agent_settings_are_reported_as_needing_a_restart_and_not_applied(live):
    app, tmp = live
    p = tmp / "kiro-crew.yaml"
    p.write_text(p.read_text().replace("default: a", "default: b").replace("approval", "approval"))
    res = app.reloader.reload()
    assert res.ok and "models" in res.restart_required and "Restart needed for: models" in res.render()
    assert app.cfg.models.default == "a"  # unchanged until restart


async def test_agent_file_settings_that_are_unusual_but_valid_load(live):
    app, tmp = live
    drop(tmp, "faq", "---\nstateful: true\nfiles: private\nmemory: private\ntools: [files]\n---\nI AM FAQ\n")
    assert app.reloader.reload().ok
    a = app.cfg.agents["faq"]
    assert a.stateful and a.private_files and a.private_memory and not a.isolated


# ---- admin chat command -----------------------------------------------------------------------------
async def test_reload_chat_command_is_admin_only(live):
    app, tmp = live
    ch = app.gateway.channels["fake"]
    drop(tmp, "faq", "---\nselectable: true\n---\nI AM FAQ\n")
    assert "Admins only" in await ch.say("/reload", user="u2")
    assert "faq" not in app.cfg.agents  # a non-admin could not trigger it
    out = await ch.say("/reload", user="u1")
    assert "added: faq" in out and "faq" in app.cfg.agents


async def test_reload_chat_command_reports_failures_to_the_admin(live):
    app, tmp = live
    drop(tmp, "faq", "---\nmodel: ghost\n---\nx\n")
    out = await app.gateway.channels["fake"].say("/reload", user="u1")
    assert "FAILED" in out and "ghost" in out


# ---- watcher ----------------------------------------------------------------------------------------
async def test_watcher_picks_up_new_changed_and_removed_files_by_itself(tmp_path):
    app = load_app(write_cfg(tmp_path, watch="true"))
    await app.gateway.start()
    app.watcher.start()
    try:
        drop(tmp_path, "faq", "---\nselectable: true\n---\nI AM FAQ\n")
        await wait_for(lambda: "faq" in app.cfg.agents, timeout=6)
        drop(tmp_path, "faq", "---\nselectable: true\ndescription: changed\n---\nI AM FAQ\n")
        await wait_for(lambda: app.cfg.agents["faq"].description == "changed", timeout=6)
        (tmp_path / "ws" / "agents" / "faq.md").unlink()
        await wait_for(lambda: "faq" not in app.cfg.agents, timeout=6)
    finally:
        await app.watcher.stop()
        app.ctx.storage.close()


async def test_watcher_survives_a_broken_file_reports_it_once_and_recovers(tmp_path, caplog):
    import logging

    app = load_app(write_cfg(tmp_path, watch="true"))
    await app.gateway.start()
    app.watcher.start()
    try:
        with caplog.at_level(logging.ERROR):
            drop(tmp_path, "faq", "---\nmodel: ghost\n---\nbroken\n")
            await asyncio.sleep(1.5)  # many poll intervals with the file still broken
            errors = [r for r in caplog.records if "watcher:" in r.message]
            assert len(errors) == 1  # reported once, not on every poll
        assert "faq" not in app.cfg.agents and "[A] ok" in await app.gateway.channels["fake"].say("ok?", user="u2")
        drop(tmp_path, "faq", "---\nselectable: true\n---\nI AM FAQ\n")  # fix it
        await wait_for(lambda: "faq" in app.cfg.agents, timeout=6)
    finally:
        await app.watcher.stop()
        app.ctx.storage.close()


async def test_an_edit_made_before_the_watcher_starts_is_not_missed(tmp_path):
    """Regression: the baseline used to be taken when the polling task first ran, swallowing earlier edits."""
    app = load_app(write_cfg(tmp_path, watch="true"))
    await app.gateway.start()
    drop(tmp_path, "early", "---\nselectable: true\n---\nI AM FAQ\n")  # after load, before the watcher exists/starts
    app.watcher.start()
    try:
        await wait_for(lambda: "early" in app.cfg.agents, timeout=6)
    finally:
        await app.watcher.stop()
        app.ctx.storage.close()


async def test_watcher_is_off_when_disabled(tmp_path):
    assert load_app(write_cfg(tmp_path, watch="false")).watcher is None


# ---- SIGHUP + `kiro-crew agent reload` ---------------------------------------------------------------
async def test_sighup_reloads_a_running_app_and_writes_a_pidfile(tmp_path):
    app = load_app(write_cfg(tmp_path, watch="false"))
    runner = asyncio.create_task(app.run())
    try:
        await wait_for(lambda: app.pidfile.exists(), timeout=8)  # written only after the SIGHUP handler exists
        drop(tmp_path, "faq", "---\nselectable: true\n---\nI AM FAQ\n")
        ok, msg = signal_running_process(app.pidfile)  # this process is "kiro-crew run" for the purposes of the test
        assert ok, msg
        await wait_for(lambda: "faq" in app.cfg.agents, timeout=6)
    finally:
        os.kill(os.getpid(), signal.SIGTERM)  # run() handles SIGTERM as a graceful stop
        await asyncio.wait_for(runner, timeout=10)
    assert not app.pidfile.exists()  # cleaned up on shutdown


def test_signal_refuses_stale_missing_and_foreign_processes(tmp_path):
    pf = tmp_path / "kiro-crew.pid"
    assert signal_running_process(pf)[0] is False and "no running" in signal_running_process(pf)[1]
    pf.write_text("not-a-number")
    assert signal_running_process(pf)[0] is False
    pf.write_text("2999999")  # no such process
    ok, msg = signal_running_process(pf)
    assert not ok and "stale" in msg
    pf.write_text(str(os.getpid()))  # a live process that is NOT kiro-crew must never be signalled
    ok, msg = signal_running_process(pf, expect="definitely-not-this-process")
    assert not ok and "not kiro-crew" in msg and "refusing" in msg


def test_looks_like_reads_the_command_line_where_available():
    assert _looks_like(os.getpid(), "python") is True
    assert _looks_like(2999999, "x") is None


def test_cli_agent_reload_without_a_running_bot_fails_cleanly(tmp_path, capsys):
    cfg = write_cfg(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["-c", str(cfg), "agent", "reload"])
    assert e.value.code == 1 and "no running kiro-crew" in capsys.readouterr().out
