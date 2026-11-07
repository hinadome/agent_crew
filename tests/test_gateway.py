import asyncio
import json

from kiro_crew.app import App
from kiro_crew.core.types import Address

from .conftest import make_config, wait_for


async def test_unauthorised_sender_is_ignored(chan):
    assert await chan.say("hello", user="stranger") == ""


async def test_normal_reply_is_chunked_to_channel_limit(chan, models):
    await chan.say("warm up")
    models["A"].reply = "word " * 60  # 300 chars, channel limit is 60
    n = len(chan.out)
    await chan.say("long please")
    parts = chan.out[n:]
    assert len(parts) > 1 and all(len(p) <= 60 for p in parts)


async def test_history_survives_restart(app, chan, models, cfg_factory):
    await chan.say("my name is Zed")
    app2 = App(cfg_factory())  # a brand-new process on the same database
    await app2.gateway.start()
    models["A"].seen.clear()
    await app2.gateway.channels["fake"].say("what is my name?")
    assert "my name is Zed" in models["A"].prompt_text()
    app2.ctx.db.close = lambda: None  # shared DB is closed by the app fixture


async def test_new_clears_history_and_model_switch_keeps_it(app, chan):
    b = app.ctx.models.profile("b")
    await chan.say("remember the word pineapple")
    assert "Switched" in await chan.say("/model b")
    assert "[B]" in await chan.say("hello again")
    assert "pineapple" in b.prompt_text()  # history preserved across the switch
    assert "fresh" in await chan.say("/new")
    b.seen.clear()
    await chan.say("anything")
    assert "pineapple" not in b.prompt_text()


async def test_model_command_validates(chan):
    assert "Unknown profile" in await chan.say("/model nope")
    assert "Available" in await chan.say("/model")


async def test_help_and_unknown_slash_goes_to_agent(chan):
    assert "/pair" in await chan.say("/help")
    assert "[A] ok" in await chan.say("/unknown thing")


async def test_identities_share_one_memory_owner(app):
    from kiro_crew.core.types import InboundMessage

    g = app.gateway
    mk = lambda uid: InboundMessage(Address("fake", "c"), uid, "x")
    assert g._user_key(mk("u1")) == g._user_key(mk("u2")) == "alice"
    assert g._user_key(mk("u9")) == "fake:u9"


async def test_shared_scope(tmp_path):
    from kiro_crew.core.types import InboundMessage

    a = App(make_config(tmp_path, memory={"scope": "shared", "auto_extract": False}))
    assert a.gateway._user_key(InboundMessage(Address("fake", "c"), "zz", "x")) == "shared"


async def test_conversations_are_independent(chan, models):
    await chan.say("secret is banana", conv="c1")
    models["A"].seen.clear()
    await chan.say("what secret?", conv="c2")
    assert "banana" not in models["A"].prompt_text()


async def test_turns_in_one_conversation_are_serialised(chan, models):
    await chan.say("warmup")
    models["A"].delay = 0.2
    t = [asyncio.create_task(chan.say(f"msg{i}")) for i in range(3)]
    await asyncio.gather(*t)
    order = [m for m in chan.out if "msg" in m]
    assert len(order) == 3


# ---- pairing ---------------------------------------------------------------------------------
async def test_pairing_flow(tmp_path):
    cfg = make_config(tmp_path, channels={"fake": {"enabled": True, "allow_from": ["u1"], "access": "pairing"}},
                      admins=["fake:u1"])
    a = App(cfg)
    await a.gateway.start()
    ch = a.gateway.channels["fake"]
    try:
        first = await ch.say("hi", user="newbie")
        assert "approve code" in first
        code = a.ctx.pairing.pending()[0].code
        assert code in first
        assert await ch.say("hi again", user="newbie") == ""  # rate-limited: no second notification
        out = await ch.say(f"/pair approve {code}", user="u1")  # u1 is admin
        assert "Approved fake:newbie" in out
        assert "[A] ok" in await ch.say("hello", user="newbie")  # now authorised, no restart
        assert "approved: fake:newbie" in await ch.say("/pair list", user="u1")
        assert "Revoked" in await ch.say("/pair revoke fake newbie", user="u1")
        assert "approve code" in await ch.say("hello", user="newbie")  # back to unauthorised
    finally:
        await a.gateway.stop()
        a.ctx.db.close()


async def test_non_admin_cannot_pair(tmp_path):
    cfg = make_config(tmp_path, channels={"fake": {"enabled": True, "allow_from": ["u1", "u2"],
                                                   "access": "pairing"}}, admins=["fake:u1"])
    a = App(cfg)
    await a.gateway.start()
    try:
        assert "Admins only" in await a.gateway.channels["fake"].say("/pair list", user="u2")
    finally:
        a.ctx.db.close()


def test_pairing_store_rules(app):
    p = app.ctx.pairing
    code, notify = p.request("fake", "x")
    assert notify and p.request("fake", "x") == (code, False)  # same code, not re-notified
    assert p.approve("wrong") is None
    assert p.approve(code.lower()).sender_id == "x" and p.is_approved("fake", "x")
    assert p.approve(code) is None  # single use
    for i in range(20):
        p.request("fake", f"s{i}")
    assert p.request("fake", "overflow") is None  # queue capped


def test_pairing_code_expires(app, monkeypatch):
    import time as _t

    p = app.ctx.pairing
    code, _ = p.request("fake", "late")
    real = _t.time
    monkeypatch.setattr("kiro_crew.security.pairing.time.time", lambda: real() + 7200)
    assert p.approve(code) is None


# ---- memory commands ---------------------------------------------------------------------------
async def test_memory_commands(app, chan):
    from kiro_crew.memory.factory import make_store

    store = make_store(app.ctx, "alice")
    await store.add("Prefers dark mode")
    await store.add("Lives in Lisbon")
    out = await chan.say("/memory")
    assert "2 memories" in out and "dark mode" in out
    assert "Lisbon" in await chan.say("/memory search where Lisbon")
    mid = store.list()[0]["id"]
    assert "Forgotten" in await chan.say(f"/memory forget {mid}")
    assert store.count() == 1
    assert "confirm" in await chan.say("/memory clear")
    assert store.count() == 1
    assert "Deleted 1" in await chan.say("/memory clear confirm")


# ---- approvals ---------------------------------------------------------------------------------
def _approval_app(tmp_path, **extra):
    return App(make_config(
        tmp_path, approval_timeout_s=extra.pop("timeout", 5),
        agents={"main": dict(tools=["files"], require_approval=["write_file"])}, **extra))


async def _run_write(a, ch, answer):
    marker = '[[tool:write_file:{"path": "a.txt", "content": "hi"}]]'
    key = Address("fake", "c1").key
    turn = asyncio.create_task(ch.say(f"please {marker}"))
    await wait_for(lambda: a.approvals.has_pending(key))
    prompt = " ".join(ch.out)  # the channel splits long messages into chunks
    reply = await ch.say(answer) if answer else None
    await turn
    return prompt, reply


async def test_approval_approve(tmp_path):
    a = _approval_app(tmp_path)
    await a.gateway.start()
    ch = a.gateway.channels["fake"]
    prompt, reply = await _run_write(a, ch, "/approve")
    assert "Approval needed" in prompt and "write_file" in prompt and reply == "Approved."
    assert (a.ctx.workspace.files_dir / "a.txt").read_text() == "hi"
    a.ctx.db.close()


async def test_approval_deny(tmp_path):
    a = _approval_app(tmp_path)
    await a.gateway.start()
    ch = a.gateway.channels["fake"]
    await _run_write(a, ch, "/deny")
    assert not (a.ctx.workspace.files_dir / "a.txt").exists()
    a.ctx.db.close()


async def test_approval_timeout_denies(tmp_path):
    a = _approval_app(tmp_path, timeout=1)
    await a.gateway.start()
    ch = a.gateway.channels["fake"]
    await _run_write(a, ch, None)
    assert not (a.ctx.workspace.files_dir / "a.txt").exists()
    assert any("auto-deny" in m or "denied" in m for m in ch.out)
    a.ctx.db.close()


async def test_approve_without_pending(chan):
    assert "Nothing is waiting" in await chan.say("/approve")


async def test_tool_without_origin_fails_closed(tmp_path):
    a = _approval_app(tmp_path)
    agent = a.factory.create(None, session_id=None, user_id="u", origin=None)
    await agent.invoke_async('[[tool:write_file:{"path": "b.txt", "content": "x"}]]')
    assert not (a.ctx.workspace.files_dir / "b.txt").exists()
    a.ctx.db.close()


async def test_prompt_contains_no_json_surprises(chan, models):
    await chan.say("hello")
    json.loads(models["A"].prompt_text())
