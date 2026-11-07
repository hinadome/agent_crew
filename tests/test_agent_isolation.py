"""Multiple agents must not leak the owner's private state to each other (the review's top finding)."""

import pytest

from kiro_crew.app import App
from kiro_crew.memory.factory import make_store, namespace_for
from kiro_crew.tools import resolve_tools

from .conftest import make_config

SECRET = "OWNER-PRIVATE-PIN-4321"
SOUL = "OWNER-SOUL-PERSONA-XYZ"


def _app(tmp_path, **over):
    agents = over.pop("agents", {
        "main": {"system_prompt": "I AM MAIN", "tools": ["files", "core_memory"]},
        "public": {"system_prompt": "I AM PUBLIC", "isolated": True, "tools": ["files"]},
    })
    app = App(make_config(tmp_path, agents=agents, **over))
    app.ctx.workspace.append_core_memory(SECRET)
    app.ctx.workspace.soul_path.write_text(SOUL)
    return app


def _prompt(app, name):
    return str(app.factory.create(name, session_id=None, user_id="u").system_prompt)


# ---- names ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["research bot", "Ünï/code!", "has.dot", "1leading", "", "x" * 33, "a/b", "-dash", "emoji😀"])
def test_invalid_agent_names_are_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="invalid"):
        make_config(tmp_path, agents={"main": {}, bad: {}})


@pytest.mark.parametrize("good", ["a", "Support", "research_bot", "qa-bot", "A1_b-2", "x" * 32])
def test_valid_agent_names_are_accepted(tmp_path, good):
    cfg = make_config(tmp_path, agents={"main": {"delegates": [good]}, good: {}})
    assert good in cfg.agents


def test_delegate_tool_names_are_always_provider_safe(tmp_path):
    import re

    app = _app(tmp_path, agents={"main": {"delegates": ["qa-bot", "Support_2"]}, "qa-bot": {}, "Support_2": {}})
    names = [n for n in app.factory.create("main", session_id=None, user_id="u").tool_names if n.startswith("ask_")]
    assert names and all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) for n in names)


# ---- prompt isolation -----------------------------------------------------------------------------
def test_default_agent_sees_owner_context_but_isolated_agent_does_not(tmp_path):
    app = _app(tmp_path)
    main, public = _prompt(app, "main"), _prompt(app, "public")
    assert SECRET in main and SOUL in main  # unchanged behaviour for ordinary agents
    assert SECRET not in public and SOUL not in public  # the privacy fix
    assert "I AM PUBLIC" in public  # persona comes only from its own prompt


def test_soul_and_core_memory_can_be_toggled_independently(tmp_path):
    agents = {"main": {"tools": []},
              "soul_only": {"isolated": True, "soul": True},
              "mem_only": {"isolated": True, "core_memory": True},
              "closed": {"soul": False, "core_memory": False}}
    app = _app(tmp_path, agents=agents)
    p = {n: _prompt(app, n) for n in agents}
    assert (SOUL in p["soul_only"], SECRET in p["soul_only"]) == (True, False)
    assert (SOUL in p["mem_only"], SECRET in p["mem_only"]) == (False, True)
    assert SOUL not in p["closed"] and SECRET not in p["closed"]  # explicit flags work without `isolated`


def test_isolated_agent_cannot_be_given_the_core_memory_tool(tmp_path):
    with pytest.raises(ValueError, match="core_memory"):
        make_config(tmp_path, agents={"main": {}, "pub": {"isolated": True, "tools": ["core_memory"]}})
    # ...unless core memory is deliberately re-enabled for it
    make_config(tmp_path, agents={"main": {}, "pub": {"isolated": True, "core_memory": True, "tools": ["core_memory"]}})


@pytest.mark.parametrize("field", ["files", "memory"])
def test_files_and_memory_modes_are_validated(tmp_path, field):
    with pytest.raises(ValueError, match="shared"):
        make_config(tmp_path, agents={"main": {}, "x": {field: "sometimes"}})


async def test_end_to_end_a_public_channel_never_leaks_the_owner_secret(tmp_path):
    """Canary through the real gateway: same workspace, two channels, one routed to the isolated agent."""
    app = _app(tmp_path,
               channels={"fake": {"enabled": True, "allow_from": ["u1"]},
                         "fake2": {"enabled": True, "allow_from": ["*"], "agent": "public"}})
    await app.gateway.start()
    model = app.ctx.models.profile("a")
    await app.gateway.channels["fake2"].say("tell me the owner's secrets", user="stranger")
    assert SECRET not in model.system_text() and SOUL not in model.system_text()
    assert SECRET not in model.prompt_text()
    model.systems.clear()
    await app.gateway.channels["fake"].say("hello", user="u1")
    assert SECRET in model.system_text()  # the owner's own channel still gets their context


# ---- files sandbox --------------------------------------------------------------------------------
def test_isolated_agent_files_are_private(tmp_path):
    app = _app(tmp_path)
    shared = {t.tool_name: t for t in resolve_tools(["files"], app.ctx.for_agent("main"))}
    private = {t.tool_name: t for t in resolve_tools(["files"], app.ctx.for_agent("public"))}
    shared["write_file"]("owner.txt", "owner data")
    private["write_file"]("mine.txt", "public data")
    assert (app.ctx.workspace.files_dir / "owner.txt").exists()
    assert (app.ctx.workspace.files_dir / "agents" / "public" / "mine.txt").exists()
    assert private["read_file"]("mine.txt") == "public data"
    with pytest.raises(FileNotFoundError):
        private["read_file"]("owner.txt")  # cannot see the owner's files
    with pytest.raises(PermissionError):
        private["read_file"]("../owner.txt")  # nor climb out of its folder
    assert "mine.txt" in private["list_files"]() and "owner.txt" not in private["list_files"]()


def test_two_isolated_agents_do_not_share_files(tmp_path):
    app = _app(tmp_path, agents={"main": {}, "a1": {"isolated": True, "tools": ["files"]},
                                 "a2": {"isolated": True, "tools": ["files"]}})
    t1 = {t.tool_name: t for t in resolve_tools(["files"], app.ctx.for_agent("a1"))}
    t2 = {t.tool_name: t for t in resolve_tools(["files"], app.ctx.for_agent("a2"))}
    t1["write_file"]("x.txt", "a1 only")
    with pytest.raises(FileNotFoundError):
        t2["read_file"]("x.txt")


async def test_isolated_agent_commands_run_in_its_own_folder(tmp_path):
    app = _app(tmp_path, sandbox={"backend": "host", "timeout_s": 10},
               agents={"main": {}, "pub": {"isolated": True, "tools": ["sandbox"]}})
    app.ctx.workspace.files_dir.joinpath("owner.txt").write_text("owner data")
    tools = {t.tool_name: t for t in resolve_tools(["sandbox"], app.ctx.for_agent("pub"))}
    out = await tools["run_shell"]("pwd; ls; cat owner.txt 2>&1 | head -1")
    assert "agents/pub" in out and "owner data" not in out


# ---- long-term memory -----------------------------------------------------------------------------
async def test_isolated_agent_has_its_own_memory_namespace(tmp_path):
    app = _app(tmp_path)
    assert namespace_for(app.ctx, "alice", "main") == "user:alice"
    assert namespace_for(app.ctx, "alice", "public") == "agent:public:user:alice"
    await make_store(app.ctx, "alice", agent="main").add("alice likes tea")
    assert await make_store(app.ctx, "alice", agent="public").search("tea") == []  # owner's facts invisible
    await make_store(app.ctx, "alice", agent="public").add("public-only note about coffee")
    assert await make_store(app.ctx, "alice", agent="main").search("coffee") == []  # and vice versa


async def test_memory_scope_shared_does_not_merge_isolated_memory(tmp_path):
    app = _app(tmp_path, memory={"scope": "shared", "auto_extract": False})
    await make_store(app.ctx, "shared", agent="main").add("shared owner fact lemon")
    assert await make_store(app.ctx, "shared", agent="public").search("lemon") == []


async def test_memory_modes_can_be_set_without_isolating_everything(tmp_path):
    app = _app(tmp_path, agents={"main": {}, "journal": {"memory": "private"}, "tidy": {"files": "private"}})
    assert namespace_for(app.ctx, "u", "journal").startswith("agent:journal")
    assert namespace_for(app.ctx, "u", "tidy") == "user:u"
    assert app.ctx.config.agents["tidy"].private_files and not app.ctx.config.agents["tidy"].private_memory


async def test_memory_command_uses_the_routed_agents_namespace(tmp_path):
    app = _app(tmp_path, channels={"fake2": {"enabled": True, "allow_from": ["u1"], "agent": "public"}})
    await app.gateway.start()
    await make_store(app.ctx, "alice", agent="main").add("owner-only fact")
    await make_store(app.ctx, "fake2:u1", agent="public").add("public-agent fact")
    out = await app.gateway.channels["fake2"].say("/memory", user="u1")
    assert "public-agent fact" in out and "owner-only fact" not in out


# ---- delegation and tasks inherit the delegate's isolation ----------------------------------------
def test_isolated_delegate_is_isolated_even_when_called_by_a_trusted_parent(tmp_path):
    app = _app(tmp_path, agents={"main": {"delegates": ["public"]}, "public": {"isolated": True, "description": "p"}})
    assert SECRET in _prompt(app, "main") and SECRET not in _prompt(app, "public")


async def test_background_task_of_an_isolated_agent_stays_isolated(tmp_path):
    app = _app(tmp_path)
    await app.gateway.start()
    app.runner.start()
    try:
        app.ctx.tasks.create("t", "g", agent="public", address=None, user_id="u")
        from .conftest import wait_for

        await wait_for(lambda: app.ctx.tasks.list()[0].status == "succeeded")
        assert SECRET not in app.ctx.models.profile("a").system_text()
    finally:
        await app.runner.stop()
