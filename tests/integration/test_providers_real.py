"""A REAL LLM through the app's provider layer. Costs a few tokens. Skips unless you opt in.

  KIRO_IT_PROVIDER   bedrock | anthropic | openai | ollama | litellm
  KIRO_IT_MODEL_ID   e.g. us.anthropic.claude-haiku-4-5-20251001-v1:0 | gpt-4o-mini | llama3.2
  KIRO_IT_API_KEY    (anthropic/openai/litellm)           optional for bedrock/ollama
  KIRO_IT_BASE_URL   (openai-compatible servers)           optional
  KIRO_IT_REGION     (bedrock)                             optional
  KIRO_IT_HOST       (ollama)                              optional

Example:  KIRO_IT_PROVIDER=openai KIRO_IT_MODEL_ID=gpt-4o-mini KIRO_IT_API_KEY=sk-... \\
          uv run pytest tests/integration/test_providers_real.py -m integration -rs
"""

import os

from kiro_crew.app import App

from ..conftest import make_config
from .conftest import need_env


def _profile() -> dict:
    env = need_env("KIRO_IT_PROVIDER", "KIRO_IT_MODEL_ID")
    client: dict = {}
    for var, key in (("KIRO_IT_API_KEY", "api_key"), ("KIRO_IT_BASE_URL", "base_url"),
                     ("KIRO_IT_REGION", "region_name"), ("KIRO_IT_HOST", "host")):
        if os.environ.get(var):
            client[key] = os.environ[var]
    return {"provider": env["KIRO_IT_PROVIDER"], "model_id": env["KIRO_IT_MODEL_ID"], "client": client,
            "params": {"max_tokens": 200} if env["KIRO_IT_PROVIDER"] in ("anthropic",) else {}}


def _app(tmp_path, profiles=None, **models_extra):
    profiles = profiles or {"real": _profile()}
    return App(make_config(tmp_path, models={"default": next(iter(profiles)), "profiles": profiles, **models_extra},
                           agents={"main": {"tools": ["time"], "system_prompt": "Be extremely brief."}}))


async def test_single_turn_round_trip(tmp_path):
    app = _app(tmp_path)
    agent = app.factory.create(None, session_id=None, user_id="u")
    result = await agent.invoke_async("Reply with exactly the single word PONG and nothing else.")
    assert "PONG" in str(result).upper()


async def test_real_model_calls_a_tool(tmp_path):
    app = _app(tmp_path)
    agent = app.factory.create(None, session_id=None, user_id="u")
    await agent.invoke_async("What is the current UTC time? You must use the current_time tool.")
    used = [b["toolUse"]["name"] for m in agent.messages for b in m.get("content", []) if "toolUse" in b]
    assert "current_time" in used


async def test_gateway_end_to_end_with_a_real_model(tmp_path):
    app = _app(tmp_path)
    await app.gateway.start()
    out = await app.gateway.channels["fake"].say("Reply with exactly the single word PONG and nothing else.")
    assert "PONG" in out.upper()


async def test_failover_from_a_broken_profile_to_the_real_one(tmp_path):
    real = _profile()
    broken = {**real, "model_id": "definitely-not-a-real-model-id"}
    app = _app(tmp_path, profiles={"broken": broken, "real": real}, fallback=["real"])
    agent = app.factory.create(None, session_id=None, user_id="u")
    result = await agent.invoke_async("Reply with exactly the single word PONG and nothing else.")
    assert "PONG" in str(result).upper()
