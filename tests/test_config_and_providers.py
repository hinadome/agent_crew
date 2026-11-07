import asyncio
from importlib import resources

import pytest
import yaml
from pydantic import ValidationError
from strands import Agent
from strands.models import ModelRouter

from kiro_crew.core.config import Config, load_config
from kiro_crew.core.registry import Registry
from kiro_crew.providers import ModelFactory, providers

from .conftest import ScriptedModel, make_config


def test_shipped_default_config_loads(tmp_path, monkeypatch):
    text = (resources.files("kiro_crew") / "default_config.yaml").read_text()
    path = tmp_path / "c.yaml"
    path.write_text(text.replace("~/.kiro-crew", str(tmp_path / "ws")))
    monkeypatch.delenv("AWS_REGION", raising=False)
    cfg = load_config(path)
    assert cfg.entry_agent == "main" and "claude" in cfg.models.profiles
    assert cfg.models.profiles["claude"].client["region_name"] == "us-east-1"  # ${VAR:-default}


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sekret")
    raw = yaml.safe_load("""
workspace: X
models: {default: a, profiles: {a: {provider: fake, model_id: m, client: {api_key: "${MY_KEY}", other: "${NOPE}"}}}}
agents: {main: {}}
""")
    from kiro_crew.core.config import _expand

    cfg = Config.model_validate(_expand(raw))
    assert cfg.models.profiles["a"].client == {"api_key": "sekret", "other": ""}


@pytest.mark.parametrize("override, message", [
    ({"bogus": 1}, "bogus"),
    ({"models": {"default": "zzz", "profiles": {"a": {"provider": "fake", "model_id": "A"}}}}, "unknown profile"),
    ({"entry_agent": "ghost"}, "entry_agent"),
    ({"agents": {"main": {"delegates": ["ghost"]}}}, "unknown agent"),
    ({"agents": {"main": {"mcp_servers": ["ghost"]}}}, "unknown mcp"),
    ({"scheduler": {"timezone": "Mars/Base"}}, ""),
    ({"channels": {"fake": {"access": "wide-open"}}}, "access"),
])
def test_invalid_config_rejected(tmp_path, override, message):
    with pytest.raises((ValidationError, ValueError, KeyError)) as e:
        make_config(tmp_path, **override)
    assert message in str(e.value)


def test_registry_basics():
    reg: Registry[int] = Registry("thing")
    reg.add("a", 1)
    assert reg.get("a") == 1 and "a" in reg and reg.names() == ["a"]
    with pytest.raises(KeyError, match="Available"):
        reg.get("missing")


def test_resolve_returns_router_only_with_fallback(tmp_path):
    plain = ModelFactory(make_config(tmp_path).models)
    assert isinstance(plain.resolve(), ScriptedModel)
    cfg = make_config(tmp_path, models={"default": "a", "fallback": ["b"], "profiles": {
        "a": {"provider": "fake", "model_id": "A"}, "b": {"provider": "fake", "model_id": "B"}}})
    f = ModelFactory(cfg.models)
    assert isinstance(f.resolve(), ModelRouter)
    assert isinstance(f.resolve("b"), ScriptedModel)  # non-default is never wrapped
    assert f.profile("a") is f.profile("a")  # cached


async def test_failover_moves_to_next_model(tmp_path):
    cfg = make_config(tmp_path, models={"default": "a", "fallback": ["b"], "profiles": {
        "a": {"provider": "fake", "model_id": "A"}, "b": {"provider": "fake", "model_id": "B"}}})
    f = ModelFactory(cfg.models)
    f.profile("a").fail = True
    # two agents share the same cached model instances, each with its own router
    out = await asyncio.gather(*[Agent(model=f.resolve(), callback_handler=None).invoke_async("hi")
                                 for _ in range(2)])
    assert all("[B]" in str(r) for r in out)


def test_builtin_providers_registered():
    assert {"bedrock", "anthropic", "openai", "ollama", "litellm"} <= set(providers.names())
