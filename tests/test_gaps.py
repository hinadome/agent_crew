"""Coverage for pieces that were previously only exercised with fakes or not at all."""

import asyncio
import json
import sys
import textwrap

import pytest

from kiro_crew.app import App
from kiro_crew.channels import channel_class
from kiro_crew.core.config import ModelProfile
from kiro_crew.core.registry import import_plugin_modules
from kiro_crew.memory.factory import make_store
from kiro_crew.providers import providers

from .conftest import make_config, wait_for


# ---- automatic memory extraction ---------------------------------------------------------------------
def _extracting_app(tmp_path, **memory):
    cfg = make_config(tmp_path, memory=dict(auto_extract=True, extract_every_n_turns=1, **memory),
                      models=dict(default="a", aux="b", profiles={"a": dict(provider="fake", model_id="A"),
                                                                  "b": dict(provider="fake", model_id="B")}))
    return App(cfg)


async def test_auto_extraction_stores_facts_from_the_aux_model(tmp_path):
    a = _extracting_app(tmp_path)
    await a.gateway.start()
    aux = a.ctx.models.profile("b")
    aux.reply = json.dumps([{"content": "User prefers green tea"}, {"content": "User lives in Berlin"}])
    ch = a.gateway.channels["fake"]
    await ch.say("I prefer green tea and live in Berlin")
    store = make_store(a.ctx, "alice")
    await wait_for(lambda: store.count() >= 2)
    facts = {m["content"] for m in store.list()}
    assert facts == {"User prefers green tea", "User lives in Berlin"}
    assert "green tea" in aux.prompt_text()  # the aux model was shown the conversation
    await ch.say("and again")  # the same facts again must not be duplicated
    await asyncio.sleep(0.5)
    assert store.count() == 2
    await a.gateway.stop()
    a.ctx.db.close()


async def test_extraction_failure_never_breaks_the_chat(tmp_path):
    a = _extracting_app(tmp_path)
    await a.gateway.start()
    a.ctx.models.profile("b").fail = True
    reply = await a.gateway.channels["fake"].say("remember I like jazz")
    assert "ok" in reply  # the user still got an answer
    await asyncio.sleep(0.3)
    assert make_store(a.ctx, "alice").count() == 0
    await a.gateway.stop()
    a.ctx.db.close()


async def test_garbage_from_the_aux_model_stores_nothing(tmp_path):
    a = _extracting_app(tmp_path)
    await a.gateway.start()
    a.ctx.models.profile("b").reply = "I am not JSON at all"
    await a.gateway.channels["fake"].say("I like jazz")
    await asyncio.sleep(0.3)
    assert make_store(a.ctx, "alice").count() == 0
    await a.gateway.stop()
    a.ctx.db.close()


# ---- the real provider builders (no network: clients are lazy) ---------------------------------------------
@pytest.mark.parametrize("provider,model_id,client", [
    ("bedrock", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", {"region_name": "us-east-1"}),
    ("openai", "gpt-4o", {"api_key": "k"}),
    ("openai", "my-model", {"api_key": "k", "base_url": "https://llm.example.com/v1"}),
    ("anthropic", "claude-sonnet-4-5", {"api_key": "k"}),
    ("ollama", "llama3.2", {"host": "http://localhost:11434"}),
    ("litellm", "gemini/gemini-2.0-flash", {"api_key": "k"}),
])
def test_real_provider_builders_construct_a_model(provider, model_id, client):
    model = providers.get(provider)(ModelProfile(provider=provider, model_id=model_id, client=client))
    cfg = model.get_config()
    assert model_id in json.dumps(cfg, default=str)


def test_openai_builder_passes_base_url_to_the_client():
    m = providers.get("openai")(ModelProfile(provider="openai", model_id="x",
                                             client={"api_key": "k", "base_url": "https://llm.example.com/v1"}))
    assert m.client_args["base_url"] == "https://llm.example.com/v1"


# ---- plugins ---------------------------------------------------------------------------------------------
def test_plugin_module_registers_a_provider(tmp_path, monkeypatch):
    (tmp_path / "my_kiro_plugin.py").write_text(textwrap.dedent('''
        from kiro_crew.providers import providers

        @providers.register("plugin-llm")
        def build(profile):
            return ("built", profile.model_id)
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert "plugin-llm" not in providers
        import_plugin_modules(["my_kiro_plugin"])
        assert providers.get("plugin-llm")(ModelProfile(provider="plugin-llm", model_id="m")) == ("built", "m")
    finally:
        providers._items.pop("plugin-llm", None)
        sys.modules.pop("my_kiro_plugin", None)


def test_missing_plugin_module_fails_loudly():
    with pytest.raises(ModuleNotFoundError):
        import_plugin_modules(["no_such_kiro_plugin_module"])


def test_app_imports_plugins_from_config(tmp_path, monkeypatch):
    (tmp_path / "cfg_plugin.py").write_text("LOADED = True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        a = App(make_config(tmp_path, plugins=["cfg_plugin"]))
        assert sys.modules["cfg_plugin"].LOADED
        a.ctx.db.close()
    finally:
        sys.modules.pop("cfg_plugin", None)


# ---- Telegram polling rules: private vs group, mentions, replies, bots ---------------------------------------
def _update(uid, text, chat="private", frm=None, reply_to=None):
    m = {"text": text, "chat": {"id": 7, "type": chat}, "from": frm or {"id": 42, "first_name": "Al"}}
    if reply_to:
        m["reply_to_message"] = {"from": {"username": reply_to}}
    return {"update_id": uid, "message": m}


async def test_telegram_group_rules():
    from kiro_crew.core.config import ChannelConfig

    ch = channel_class("telegram")("telegram", ChannelConfig(token="123:abc", allow_from=["*"]))
    batches = [[
        _update(1, "hello"),                                                     # DM: handled
        _update(2, "hello group", chat="group"),                                 # group, no mention: ignored
        _update(3, "hey @KiroBot what time is it", chat="supergroup"),           # mention: handled, stripped
        _update(4, "and this?", chat="group", reply_to="kirobot"),               # reply to the bot: handled
        _update(5, "bot noise", frm={"id": 1, "is_bot": True}),                  # from a bot: ignored
        _update(6, ""),                                                          # no text: ignored
    ]]
    done = asyncio.Event()

    async def fake_call(method, **params):
        if method == "getMe":
            return {"username": "KiroBot"}
        if batches:
            return batches.pop(0)
        done.set()
        await asyncio.sleep(3600)  # long poll with nothing new

    ch._call = fake_call  # type: ignore[method-assign]
    got = []

    async def handler(msg):
        got.append(msg)

    await ch.start(handler)
    await asyncio.wait_for(done.wait(), 5)
    await ch.stop()
    assert [(m.text, m.is_group) for m in got] == [
        ("hello", False), ("hey what time is it", True), ("and this?", True)]
    assert all(m.address.channel == "telegram" and m.address.conversation_id == "7" for m in got)


# ---- Slack / Discord inbound rules (pure functions; no network) --------------------------------------------
def test_slack_inbound_rules():
    pytest.importorskip("slack_bolt")
    from kiro_crew.channels.slack_channel import slack_inbound

    dm = slack_inbound({"channel": "D1", "channel_type": "im", "user": "U1", "text": "hi", "ts": "1.0"})
    assert dm and (dm.text, dm.is_group, dm.address.thread_id) == ("hi", False, None)
    ch = slack_inbound({"channel": "C1", "channel_type": "channel", "user": "U1", "text": "<@U0BOT> status?",
                        "ts": "2.0"})
    assert ch and (ch.text, ch.is_group, ch.address.thread_id) == ("status?", True, "2.0")  # threads under it
    th = slack_inbound({"channel": "C1", "user": "U1", "text": "<@U0BOT> more", "ts": "3.0", "thread_ts": "2.0"})
    assert th and th.address.thread_id == "2.0"  # stays in the existing thread
    assert slack_inbound({"channel": "C1", "user": "U1", "text": "x", "ts": "1", "bot_id": "B1"}) is None
    assert slack_inbound({"channel": "C1", "user": "U1", "text": "x", "ts": "1", "subtype": "message_changed"}) is None
    assert slack_inbound({"channel": "C1", "user": "U1", "text": "<@U0BOT>", "ts": "1"}) is None


def test_discord_inbound_rules():
    pytest.importorskip("discord")
    from types import SimpleNamespace as NS

    from kiro_crew.channels.discord_channel import discord_inbound

    me = NS(id=99, bot=True)
    human = NS(id=5, bot=False, display_name="Al")

    def msg(content, guild=None, mentions=(), author=human):
        return NS(content=content, guild=guild, mentions=list(mentions), author=author, channel=NS(id=123))

    dm = discord_inbound(msg("hello"), me)
    assert dm and (dm.text, dm.is_group, dm.user_id, dm.address.conversation_id) == ("hello", False, "5", "123")
    assert discord_inbound(msg("hello all", guild=NS()), me) is None  # server channel without a mention
    g = discord_inbound(msg("<@!99> what's up", guild=NS(), mentions=[me]), me)
    assert g and (g.text, g.is_group) == ("what's up", True)
    assert discord_inbound(msg("hi", author=NS(id=7, bot=True, display_name="b")), me) is None
    assert discord_inbound(msg("hi"), None) is None  # not logged in yet
    assert discord_inbound(msg("<@99>", guild=NS(), mentions=[me]), me) is None  # mention only, no text


# ---- third-party plugins via entry points --------------------------------------------------------------------
def test_entry_point_plugins_load_and_a_broken_one_is_skipped(monkeypatch, caplog):
    from types import SimpleNamespace as NS

    from kiro_crew.core import registry as R

    reg = R.Registry("widget")
    good = NS(name="good", load=lambda: "GOOD")

    def boom():
        raise ImportError("missing dependency")

    bad = NS(name="bad", load=boom)
    seen_groups = []

    def fake_entry_points(group):
        seen_groups.append(group)
        return [good, bad]

    monkeypatch.setattr(R, "entry_points", fake_entry_points)
    assert reg.names() == ["good"] and reg.get("good") == "GOOD"
    assert seen_groups == ["kiro_crew.widgets"]
    assert "Failed to load widget plugin 'bad'" in caplog.text
    reg.names()
    assert seen_groups == ["kiro_crew.widgets"]  # loaded once
