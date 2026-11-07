"""Which agent answers: routes, channel/entry defaults, and the user-driven /agent switch."""

import pytest

from kiro_crew.app import App
from kiro_crew.core.types import Address, InboundMessage
from kiro_crew.memory.factory import make_store
from kiro_crew.routing import matches, resolve_agent, switchable_agents

from .conftest import make_config, wait_for

AGENTS = {
    "main": {"system_prompt": "I AM MAIN", "tools": ["time", "tasks"]},
    "support": {"system_prompt": "I AM SUPPORT", "selectable": True, "description": "customer support"},
    "research": {"system_prompt": "I AM RESEARCH", "selectable": True},
    "secret": {"system_prompt": "I AM SECRET"},  # not selectable
    "public": {"system_prompt": "I AM PUBLIC", "isolated": True, "selectable": True},
}


def cfg(tmp_path, **over):
    base = {"agents": AGENTS, "channels": {"fake": {"enabled": True, "allow_from": ["u1", "u2"]},
                                           "fake2": {"enabled": True, "allow_from": ["u1"], "agent": "research"}},
            "admins": ["fake:u1"]}
    base.update(over)
    return make_config(tmp_path, **base)


def msg(channel="fake", conv="c1", user="u1", group=False):
    return InboundMessage(Address(channel, conv), user, "hi", is_group=group)


def who(model):
    """Which agent produced the latest model call, from its system prompt."""
    text = model.systems[-1].__repr__()
    for name in ("MAIN", "SUPPORT", "RESEARCH", "SECRET", "PUBLIC"):
        if f"I AM {name}" in text:
            return name.lower()
    return None


# ---- pure decision logic ---------------------------------------------------------------------------
def test_defaults_entry_then_channel(tmp_path):
    c = cfg(tmp_path)
    assert resolve_agent(c, c.channels["fake"], msg()).agent == "main"
    r = resolve_agent(c, c.channels["fake2"], msg("fake2"))
    assert (r.agent, r.reason) == ("research", "channel")


def test_precedence_override_over_route_over_channel_over_entry(tmp_path):
    c = cfg(tmp_path, routes=[{"agent": "support", "channel": "fake2"}])
    assert resolve_agent(c, c.channels["fake2"], msg("fake2")).reason == "route[0]"  # route beats channel default
    r = resolve_agent(c, c.channels["fake2"], msg("fake2"), override="public")
    assert (r.agent, r.reason) == ("public", "override")  # a user's choice beats routes


def test_stale_override_is_ignored(tmp_path):
    c = cfg(tmp_path)
    assert resolve_agent(c, c.channels["fake"], msg(), override="deleted-agent").reason == "entry"


def test_first_matching_route_wins_and_fields_are_anded(tmp_path):
    c = cfg(tmp_path, routes=[
        {"agent": "public", "channel": "fake", "group": True, "user": "u2"},   # needs ALL three
        {"agent": "support", "channel": "fake", "group": True},
        {"agent": "research", "conversation": "special"},
    ])
    ch = c.channels["fake"]
    assert resolve_agent(c, ch, msg(group=True, user="u2")).agent == "public"
    assert resolve_agent(c, ch, msg(group=True, user="u1")).agent == "support"  # first rule not fully matched
    assert resolve_agent(c, ch, msg(group=False)).agent == "main"  # DM matches no rule
    assert resolve_agent(c, ch, msg(conv="special")).agent == "research"


@pytest.mark.parametrize("rule, message, expect", [
    ({"agent": "x", "channel": "fake"}, msg("fake"), True),
    ({"agent": "x", "channel": "fake"}, msg("fake2"), False),
    ({"agent": "x", "user": "u1"}, msg(user="u2"), False),
    ({"agent": "x", "conversation": "c9"}, msg(conv="c9"), True),
    ({"agent": "x", "group": False}, msg(group=False), True),
    ({"agent": "x", "group": False}, msg(group=True), False),
])
def test_matches(rule, message, expect):
    from kiro_crew.core.config import RouteRule

    assert matches(RouteRule(**rule), message) is expect


def test_switchable_agents_respects_selectable_allow_list_and_admin(tmp_path):
    c = cfg(tmp_path, channels={"fake": {"enabled": True, "agents_allowed": ["support", "secret"]},
                                "fake2": {"enabled": True}})
    assert switchable_agents(c, c.channels["fake"], is_admin=False) == ["support"]  # secret is not selectable
    assert switchable_agents(c, c.channels["fake2"], is_admin=False) == ["public", "research", "support"]
    assert switchable_agents(c, c.channels["fake"], is_admin=True) == sorted(AGENTS)


# ---- through the real gateway ----------------------------------------------------------------------
@pytest.fixture
async def gw(tmp_path):
    app = App(cfg(tmp_path, routes=[{"agent": "support", "channel": "fake", "group": True}]))
    await app.gateway.start()
    yield app
    app.ctx.storage.close()


async def test_routes_pick_the_agent_per_message(gw):
    ch, model = gw.gateway.channels["fake"], gw.ctx.models.profile("a")
    await ch.say("hi", group=False)
    assert who(model) == "main"
    await ch.say("hi", group=True, conv="grp")
    assert who(model) == "support"  # same channel, a group chat -> routed by the rule
    await gw.gateway.channels["fake2"].say("hi")
    assert who(model) == "research"  # the channel's own default


async def test_agent_command_lists_only_what_a_user_may_pick(gw):
    ch = gw.gateway.channels["fake"]
    out = await ch.say("/agent", user="u2")  # u2 is not an admin
    assert "Current agent: main (entry)" in out
    assert "support - customer support" in out and "research" in out and "public [isolated" in out
    assert "secret" not in out  # not selectable
    admin_out = await ch.say("/agents", user="u1")
    assert "secret" in admin_out


async def test_switch_agent_changes_who_answers_and_histories_stay_separate(gw):
    ch, model = gw.gateway.channels["fake"], gw.ctx.models.profile("a")
    await ch.say("my favourite fruit is kumquat", user="u2")
    assert "Switched this conversation to support" in await ch.say("/agent support", user="u2")
    model.seen.clear()
    await ch.say("which fruit?", user="u2")
    assert who(model) == "support" and "kumquat" not in model.prompt_text()  # support has its own, empty history
    await ch.say("support-only note: pineapple", user="u2")
    assert "Back to the default" in await ch.say("/agent reset", user="u2")
    model.seen.clear()
    await ch.say("which fruit?", user="u2")
    assert who(model) == "main"
    assert "kumquat" in model.prompt_text() and "pineapple" not in model.prompt_text()  # main's history was kept


async def test_switching_to_a_non_selectable_or_unknown_agent_is_refused(gw):
    ch, model = gw.gateway.channels["fake"], gw.ctx.models.profile("a")
    for target in ("secret", "ghost"):
        out = await ch.say(f"/agent {target}", user="u2")
        assert "can't switch" in out
    await ch.say("hi", user="u2")
    assert who(model) == "main"


async def test_admins_may_switch_to_any_agent(gw):
    ch, model = gw.gateway.channels["fake"], gw.ctx.models.profile("a")
    assert "Switched" in await ch.say("/agent secret", user="u1")
    await ch.say("hi", user="u1")
    assert who(model) == "secret"


async def test_channel_agents_allowed_narrows_what_can_be_picked(tmp_path):
    app = App(cfg(tmp_path, channels={"fake": {"enabled": True, "allow_from": ["u2"], "agents_allowed": ["support"]}}))
    await app.gateway.start()
    ch = app.gateway.channels["fake"]
    assert "Switched" in await ch.say("/agent support", user="u2")
    assert "can't switch" in await ch.say("/agent research", user="u2")  # selectable, but not allowed here


async def test_override_survives_a_restart(tmp_path):
    a1 = App(cfg(tmp_path))
    await a1.gateway.start()
    await a1.gateway.channels["fake"].say("/agent support", user="u2")
    a2 = App(cfg(tmp_path))  # a new process on the same storage
    await a2.gateway.start()
    await a2.gateway.channels["fake"].say("hi", user="u2")
    assert who(a2.ctx.models.profile("a")) == "support"


async def test_new_clears_every_agents_history_but_keeps_the_choice(gw):
    ch, model = gw.gateway.channels["fake"], gw.ctx.models.profile("a")
    await ch.say("remember zebra", user="u2")
    await ch.say("/agent support", user="u2")
    await ch.say("remember giraffe", user="u2")
    await ch.say("/new", user="u2")
    model.seen.clear()
    await ch.say("anything", user="u2")
    assert who(model) == "support"  # the selection is a setting, like /model, so it persists
    assert "giraffe" not in model.prompt_text() and "zebra" not in model.prompt_text()
    await ch.say("/agent reset", user="u2")
    model.seen.clear()
    await ch.say("anything", user="u2")
    assert "zebra" not in model.prompt_text()  # main's history was wiped as well


async def test_an_override_pointing_at_a_removed_agent_does_not_break_the_chat(gw):
    key = Address("fake", "c1").key
    gw.ctx.settings.set_agent(key, "removed-last-week")
    out = await gw.gateway.channels["fake"].say("hello", user="u2")
    assert "[A] ok" in out and who(gw.ctx.models.profile("a")) == "main"


async def test_background_task_runs_as_the_agent_the_user_switched_to(gw):
    ch = gw.gateway.channels["fake"]
    gw.runner.start()
    try:
        await ch.say("/agent support", user="u1")
        # `support` has no task tools in this config; give the switched agent one via config
        gw.ctx.config.agents["support"].tools = ["tasks"]
        gw.gateway.evict()
        await ch.say('[[tool:start_task:{"title": "T", "goal": "g"}]]', user="u1")
        await wait_for(lambda: bool(gw.ctx.tasks.list()))
        assert gw.ctx.tasks.list()[0].agent == "support"
        await wait_for(lambda: gw.ctx.tasks.list()[0].status == "succeeded")
    finally:
        await gw.runner.stop()


async def test_memory_command_follows_the_agent_in_use(gw):
    ch = gw.gateway.channels["fake"]
    await make_store(gw.ctx, "alice", agent="main").add("main-agent fact")
    await make_store(gw.ctx, "alice", agent="public").add("public-agent fact")
    assert "main-agent fact" in await ch.say("/memory", user="u1")
    await ch.say("/agent public", user="u1")
    out = await ch.say("/memory", user="u1")
    assert "public-agent fact" in out and "main-agent fact" not in out
