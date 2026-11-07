"""Drop-in agent files (agents/*.md), config validation of agent references, and the management helpers."""

import textwrap

import pytest

from kiro_crew.agents import manage
from kiro_crew.core import agentfiles
from kiro_crew.core.config import load_config

from .conftest import make_config

BASE = textwrap.dedent("""
    workspace: {ws}
    entry_agent: main
    models:
      default: a
      profiles:
        a: {{provider: fake, model_id: A}}
    agents:
      main:
        system_prompt: I AM MAIN
    channels:
      fake: {{enabled: true, allow_from: [u1]}}
""")


@pytest.fixture
def proj(tmp_path):
    ws = tmp_path / "ws"
    cfg = tmp_path / "kiro-crew.yaml"
    cfg.write_text(BASE.format(ws=ws))
    (ws / "agents").mkdir(parents=True)
    return cfg, ws / "agents"


def write(directory, name, text):
    (directory / f"{name}.md").write_text(textwrap.dedent(text).lstrip())


# ---- parsing ---------------------------------------------------------------------------------------
def test_front_matter_and_body():
    meta, body = agentfiles.split_front_matter("---\ndescription: hi\ntools: [web]\n---\nThe prompt.\nSecond line.\n")
    assert meta == {"description": "hi", "tools": ["web"]} and body == "The prompt.\nSecond line."


def test_no_front_matter_means_all_prompt():
    assert agentfiles.split_front_matter("Just a prompt\n") == ({}, "Just a prompt")
    assert agentfiles.split_front_matter("--- not a fence\nx") == ({}, "--- not a fence\nx")


@pytest.mark.parametrize("text, message", [
    ("---\ndescription: x\nbody but never closed", "never closed"),
    ("---\n: : bad: yaml: [\n---\nprompt", "invalid YAML"),
    ("---\n- a\n- list\n---\nprompt", "mapping"),
])
def test_malformed_front_matter_is_reported(text, message):
    with pytest.raises(agentfiles.AgentFileError, match=message):
        agentfiles.split_front_matter(text, "f.md")


def test_render_and_read_roundtrip(tmp_path):
    p = tmp_path / "faq.md"
    p.write_text(agentfiles.render_agent_file({"description": "d", "tools": ["web"], "isolated": True}, "Be nice."))
    name, meta = agentfiles.read_agent_file(p)
    assert name == "faq" and meta == {"description": "d", "tools": ["web"], "isolated": True, "system_prompt": "Be nice."}


def test_env_expansion_applies_to_settings_but_never_to_the_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_X", "fast")
    p = tmp_path / "x.md"
    p.write_text("---\nmodel: ${MODEL_X}\n---\nUse ${NOT_EXPANDED} literally.\n")
    from kiro_crew.core.config import _expand

    _, meta = agentfiles.read_agent_file(p, _expand)
    assert meta["model"] == "fast" and "${NOT_EXPANDED}" in meta["system_prompt"]


def test_prompt_in_both_body_and_setting_is_ambiguous(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nsystem_prompt: a\n---\nb\n")
    with pytest.raises(agentfiles.AgentFileError, match="both"):
        agentfiles.read_agent_file(p)


def test_only_markdown_files_not_starting_with_underscore_or_dot_are_agents(tmp_path):
    for n in ("good.md", "_parked.md", ".hidden.md", "notes.txt", "other.yaml"):
        (tmp_path / n).write_text("p")
    assert [p.name for p in agentfiles.agent_files(tmp_path)] == ["good.md"]
    assert agentfiles.agent_files(tmp_path / "missing") == []


def test_file_name_must_be_a_valid_agent_name(tmp_path):
    p = tmp_path / "bad name.md"
    p.write_text("x")
    with pytest.raises(agentfiles.AgentFileError, match="not a valid agent name"):
        agentfiles.read_agent_file(p)


# ---- loading into the config -----------------------------------------------------------------------
def test_drop_in_agent_is_merged_with_its_source_recorded(proj):
    cfg_path, agents = proj
    write(agents, "faq", """
        ---
        description: Answers FAQs
        isolated: true
        ---
        You answer FAQs.
    """)
    cfg = load_config(cfg_path)
    assert set(cfg.agents) == {"main", "faq"}
    assert cfg.agents["faq"].system_prompt == "You answer FAQs." and cfg.agents["faq"].isolated
    assert cfg.agent_sources["main"] == "yaml" and cfg.agent_sources["faq"].endswith("faq.md")


def test_delegate_of_adds_the_agent_to_a_parent_without_editing_the_parent(proj):
    cfg_path, agents = proj
    write(agents, "helper", "---\ndelegate_of: [main]\ndescription: helps\n---\nHelp.\n")
    cfg = load_config(cfg_path)
    assert cfg.agents["main"].delegates == ["helper"]  # parent is defined in YAML and was not touched on disk
    assert "helper" not in cfg_path.read_text()


def test_delegate_of_accepts_a_single_string_and_is_idempotent(proj):
    cfg_path, agents = proj
    write(agents, "h1", "---\ndelegate_of: main\n---\nx\n")
    assert load_config(cfg_path).agents["main"].delegates == ["h1"]


def test_delegate_of_unknown_parent_names_the_file(proj):
    cfg_path, agents = proj
    write(agents, "orphan", "---\ndelegate_of: [ghost]\n---\nx\n")
    with pytest.raises(agentfiles.AgentFileError, match=r"orphan\.md.*ghost"):
        load_config(cfg_path)


def test_same_agent_in_yaml_and_file_is_an_error(proj):
    cfg_path, agents = proj
    write(agents, "main", "---\ndescription: dup\n---\nx\n")
    with pytest.raises(agentfiles.AgentFileError, match=r"both in the YAML config and in .*main\.md"):
        load_config(cfg_path)


def test_unknown_setting_in_a_file_names_the_file(proj):
    cfg_path, agents = proj
    write(agents, "typo", "---\ndescriptoin: oops\n---\nx\n")
    with pytest.raises(agentfiles.AgentFileError, match=r"typo\.md"):
        load_config(cfg_path)


def test_system_prompt_file_for_yaml_and_drop_in_agents(proj):
    cfg_path, agents = proj
    (cfg_path.parent / "prompt.txt").write_text("YAML AGENT PROMPT FROM FILE\n")
    cfg_path.write_text(cfg_path.read_text().replace("system_prompt: I AM MAIN", "system_prompt_file: prompt.txt"))
    (agents / "side.txt").write_text("DROP-IN PROMPT FROM FILE")
    write(agents, "side", "---\nsystem_prompt_file: side.txt\n---\n")
    cfg = load_config(cfg_path)
    assert cfg.agents["main"].system_prompt == "YAML AGENT PROMPT FROM FILE"
    assert cfg.agents["side"].system_prompt == "DROP-IN PROMPT FROM FILE"


def test_missing_system_prompt_file_is_reported(proj):
    cfg_path, _ = proj
    cfg_path.write_text(cfg_path.read_text().replace("system_prompt: I AM MAIN", "system_prompt_file: nope.txt"))
    with pytest.raises(agentfiles.AgentFileError, match="nope.txt"):
        load_config(cfg_path)


def test_custom_agent_files_directory(proj, tmp_path):
    cfg_path, _ = proj
    other = tmp_path / "elsewhere"
    other.mkdir()
    write(other, "remote", "---\ndescription: r\n---\nx\n")
    cfg_path.write_text(cfg_path.read_text() + f"agent_files: {{dir: {other}}}\n")
    assert "remote" in load_config(cfg_path).agents


# ---- config cross-reference validation (previously unchecked) --------------------------------------
@pytest.mark.parametrize("over, message", [
    ({"channels": {"fake": {"enabled": True, "agent": "ghost"}}}, "channels.fake.agent"),
    ({"routes": [{"agent": "ghost", "channel": "fake"}]}, "routes\\[0\\].agent"),
    ({"tasks": {"agent": "ghost"}}, "tasks.agent"),
    ({"heartbeat": {"agent": "ghost"}}, "heartbeat.agent"),
    ({"channels": {"fake": {"enabled": True, "agents_allowed": ["ghost"]}}}, "agents_allowed"),
    ({"routes": [{"agent": "main"}]}, "no match field"),
    ({"agents": {"main": {"delegates": ["main"]}}}, "itself"),
])
def test_dangling_agent_references_fail_at_load_not_at_runtime(tmp_path, over, message):
    with pytest.raises(ValueError, match=message):
        make_config(tmp_path, **over)


def test_delegation_cycles_warn_once_but_are_allowed(tmp_path):
    with pytest.warns(UserWarning, match="delegation cycle") as rec:
        make_config(tmp_path, agents={"main": {"delegates": ["a"]}, "a": {"delegates": ["b"]}, "b": {"delegates": ["a"]}})
    assert len([w for w in rec if "delegation cycle" in str(w.message)]) == 1


def test_acyclic_delegation_does_not_warn(tmp_path, recwarn):
    make_config(tmp_path, agents={"main": {"delegates": ["a", "b"]}, "a": {"delegates": ["b"]}, "b": {}})
    assert not [w for w in recwarn if "delegation cycle" in str(w.message)]


# ---- management helpers ----------------------------------------------------------------------------
def test_validate_reports_ok_and_failures(proj):
    cfg_path, agents = proj
    assert manage.validate(cfg_path).message == "OK: 1 agents (1 in YAML, 0 drop-in files)"
    write(agents, "broken", "---\nmodel: nope\n---\nx\n")
    bad = manage.validate(cfg_path)
    assert not bad.ok and "unknown model profile" in bad.message


def test_scaffold_creates_a_valid_agent_and_template_flags(proj):
    cfg_path, agents = proj
    path = manage.scaffold(cfg_path, "faq", prompt="Answer FAQs.", template="public", delegate_of=["main"],
                           description="FAQ", tools=["files"])
    assert path == agents / "faq.md"
    cfg = load_config(cfg_path)
    a = cfg.agents["faq"]
    assert a.isolated and a.selectable and a.tools == ["files"] and a.description == "FAQ"
    assert cfg.agents["main"].delegates == ["faq"]


def test_scaffold_rolls_back_when_the_result_would_not_load(proj):
    cfg_path, agents = proj
    with pytest.raises(ValueError, match="not created"):
        manage.scaffold(cfg_path, "bad", prompt="x", model="no-such-profile")
    assert not (agents / "bad.md").exists()  # nothing left behind to break the running bot's next reload
    assert manage.validate(cfg_path).ok


def test_scaffold_refuses_duplicates_and_bad_names_and_templates(proj):
    cfg_path, _ = proj
    with pytest.raises(ValueError, match="already exists"):
        manage.scaffold(cfg_path, "main", prompt="x")
    with pytest.raises(ValueError, match="invalid agent name"):
        manage.scaffold(cfg_path, "has space", prompt="x")
    with pytest.raises(ValueError, match="unknown template"):
        manage.scaffold(cfg_path, "ok", prompt="x", template="fancy")


def test_remove_only_touches_drop_in_files_and_restores_on_dangling_references(proj):
    cfg_path, agents = proj
    manage.scaffold(cfg_path, "kid", prompt="x", delegate_of=["main"])
    manage.scaffold(cfg_path, "other", prompt="x")
    with pytest.raises(ValueError, match="defined in the YAML"):
        manage.remove(cfg_path, "main")
    with pytest.raises(ValueError, match="no such agent"):
        manage.remove(cfg_path, "ghost")
    # a YAML agent that delegates to a drop-in file: removing the file would dangle -> refused and restored
    cfg_path.write_text(cfg_path.read_text().replace("system_prompt: I AM MAIN",
                                                     "system_prompt: I AM MAIN\n    delegates: [other]"))
    with pytest.raises(ValueError, match="still referenced"):
        manage.remove(cfg_path, "other")
    assert (agents / "other.md").exists()  # restored
    manage.remove(cfg_path, "kid")
    assert not (agents / "kid.md").exists() and manage.validate(cfg_path).ok


def test_overview_describes_sources_flags_and_what_reaches_each_agent(proj):
    cfg_path, _ = proj
    manage.scaffold(cfg_path, "faq", prompt="x", template="public", delegate_of=["main"])
    cfg_path.write_text(cfg_path.read_text().replace("channels:", "routes:\n  - {agent: faq, channel: fake, group: true}\nchannels:"))
    rows = {r["name"]: r for r in manage.overview(load_config(cfg_path))}
    assert rows["main"]["source"] == "yaml" and "entry_agent" in rows["main"]["routed_from"]
    faq = rows["faq"]
    assert faq["source"] == "file" and faq["isolated"] and faq["private_memory"] and not faq["core_memory"]
    assert faq["delegate_of"] == ["main"]
    assert any(r.startswith("route[0]") and "group=True" in r for r in faq["routed_from"])
