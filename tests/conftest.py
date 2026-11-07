"""Shared fixtures: a scripted fake LLM, a fake chat channel, and app builders."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from strands.models.model import Model

from kiro_crew.app import App
from kiro_crew.channels import Channel, channel_registry
from kiro_crew.core.config import Config
from kiro_crew.core.types import Address, InboundMessage
from kiro_crew.providers import providers

# A prompt containing  [[tool:NAME:{"json": "args"}]]  makes the fake model call that tool once.
TOOL_MARKER = re.compile(r"\[\[tool:(\w+):(\{.*?\})\]\]")


class ScriptedModel(Model):
    """Deterministic LLM stand-in. Echoes input, or calls a tool when asked via TOOL_MARKER."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.seen: list[Any] = []  # every messages list passed to stream()
        self.systems: list[Any] = []  # the system prompt given on each call
        self.fail = False  # raise instead of answering
        self.delay = 0.0  # sleep before answering (for timeouts)
        self.reply: str | None = None  # fixed reply text
        self.replies: list[str] = []  # consumed one per call before falling back to `reply`
        self.always_tool: tuple[str, str] | None = None  # call this tool (name, json args) on every call
        self.n_tool_calls = 0  # tool calls this model has requested

    def update_config(self, **kwargs: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": self.tag}

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def stream(self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, *,
                     tool_choice: Any = None, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.seen.append(copy.deepcopy(messages))
        self.systems.append((system_prompt, kwargs.get("system_prompt_content")))
        if self.fail:
            raise RuntimeError("model exploded")
        if self.delay:
            await asyncio.sleep(self.delay)
        last = messages[-1]
        text = " ".join(b.get("text", "") for b in last["content"] if "text" in b)
        got_tool_result = any("toolResult" in b for b in last["content"])
        yield {"messageStart": {"role": "assistant"}}
        marker = None if got_tool_result else TOOL_MARKER.search(text)
        tool_call: tuple[str, str] | None = (marker.group(1), marker.group(2)) if marker else None
        if self.always_tool:  # a model that never stops calling tools
            tool_call = self.always_tool
        if tool_call:
            self.n_tool_calls += 1
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tu_{tool_call[0]}_{self.n_tool_calls}",
                                                               "name": tool_call[0]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": tool_call[1]}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            out = self.replies.pop(0) if self.replies else self.reply if self.reply is not None else f"[{self.tag}] ok: {text[:80]}"
            yield {"contentBlockDelta": {"delta": {"text": out}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}

    def system_text(self) -> str:
        """Every system prompt this model has been given (to assert what an agent was or was not told)."""
        return json.dumps(self.systems, default=str)

    def prompt_text(self) -> str:
        """All text the model has been shown so far (for asserting on injected memory etc.)."""
        return json.dumps(self.seen)


class FakeChannel(Channel):
    max_len = 60

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self.out: list[str] = []
        self.handler: Callable[..., Any] | None = None

    async def start(self, on_message: Any) -> None:
        self.handler = on_message

    async def stop(self) -> None:
        pass

    async def send(self, msg: Any) -> None:
        self.out.append(msg.text)

    async def say(self, text: str, user: str = "u1", conv: str = "c1", group: bool = False) -> str:
        """Send a message as ``user`` and return everything the bot replied (joined)."""
        n = len(self.out)
        assert self.handler is not None
        # address by this channel's own name so several fake channels (fake, fake2, ...) are distinguishable
        await self.handler(InboundMessage(Address(self.name, conv), user, text, user_name=user, is_group=group))
        return " ".join(self.out[n:])


channel_registry.add("fake", FakeChannel)
channel_registry.add("fake2", FakeChannel)

MODELS: dict[str, ScriptedModel] = {}


@providers.register("fake")
def _build(profile: Any) -> ScriptedModel:
    return MODELS.setdefault(profile.model_id, ScriptedModel(profile.model_id))


@pytest.fixture(autouse=True)
def _reset_models() -> None:
    MODELS.clear()


@pytest.fixture(autouse=True)
def _reset_logging_handlers():
    """Keep logging global state from leaking between tests (ring buffer + our handlers)."""
    import logging as _logging

    from kiro_crew.core import logging as _klog

    yield
    root = _logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_kiro", False)]:
        root.removeHandler(h)
    _klog._RING = None
    _klog._POLICY = _klog.RedactionPolicy()
    for name in ("strands", "kiro_crew", *_klog._NOISY_THIRD_PARTY):
        _logging.getLogger(name).setLevel(_logging.NOTSET)


@pytest.fixture
def models() -> dict[str, ScriptedModel]:
    return MODELS


def make_config(tmp_path: Any, **over: Any) -> Config:
    base: dict[str, Any] = dict(
        workspace=str(tmp_path / "ws"), entry_agent="main",
        models=dict(default="a", profiles={"a": dict(provider="fake", model_id="A"),
                                           "b": dict(provider="fake", model_id="B")}),
        memory=dict(auto_extract=False), tasks=dict(workers=1, max_attempts=2),
        scheduler=dict(tick_s=1), housekeeping=dict(enabled=False),
        agents=dict(main=dict(tools=["time", "tasks", "schedule"], system_prompt="You are main.",
                              delegates=["helper"]),
                    helper=dict(description="helps", system_prompt="You help.")),
        channels=dict(fake=dict(enabled=True, allow_from=["u1"])),
        identities={"alice": ["fake:u1", "fake:u2"]},
    )
    base.update(over)
    return Config.model_validate(base)


@pytest.fixture
def cfg_factory(tmp_path: Any) -> Callable[..., Config]:
    return lambda **over: make_config(tmp_path, **over)


@pytest.fixture
async def app(cfg_factory: Callable[..., Config]) -> AsyncIterator[App]:
    a = App(cfg_factory())
    await a.gateway.start()
    if a.runner:
        a.runner.start()
    yield a
    if a.runner:
        await a.runner.stop()
    await a.gateway.stop()
    a.ctx.db.close()


@pytest.fixture
def chan(app: App) -> FakeChannel:
    return app.gateway.channels["fake"]  # type: ignore[return-value]


async def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)
