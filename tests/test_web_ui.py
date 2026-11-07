import asyncio
import json
import logging
import socket

import httpx
import pytest

from kiro_crew.app import App

from .conftest import make_config


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def web_app(tmp_path):
    port = _free_port()
    app = App(make_config(tmp_path, web_ui={"enabled": True, "host": "127.0.0.1", "port": port, "token": "secret"},
                          logging={"level": "DEBUG", "areas": {"gateway": "debug", "agents": "debug"}}))
    await app.gateway.start()
    if app.runner:
        app.runner.start()
    await app.web.start()
    app._base = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]
    yield app
    if app.runner:
        await app.runner.stop()
    await app.web.stop()
    await app.gateway.stop()
    app.ctx.db.close()
    # tear down logging handlers so other tests are unaffected
    root = logging.getLogger()
    for h in [h for h in root.handlers if getattr(h, "_kiro", False)]:
        root.removeHandler(h)


async def test_requires_token(web_app):
    async with httpx.AsyncClient() as h:
        assert (await h.get(web_app._base + "/api/logs")).status_code == 401
        assert (await h.get(web_app._base + "/api/logs", params={"token": "wrong"})).status_code == 401
        assert (await h.get(web_app._base + "/api/state", params={"token": "secret"})).status_code == 200


async def test_index_served(web_app):
    async with httpx.AsyncClient() as h:
        r = await h.get(web_app._base + "/")
        assert r.status_code == 200 and "kiro-crew" in r.text and "text/html" in r.headers["content-type"]


async def test_logs_api_captures_debug_trace(web_app):
    await web_app.gateway.channels["fake"].say("hello there")
    async with httpx.AsyncClient() as h:
        events = (await h.get(web_app._base + "/api/logs", params={"token": "secret", "limit": 2000})).json()
    msgs = [e["message"] for e in events]
    assert any("inbound message" in m for m in msgs)
    assert any("agent reply" in m for m in msgs)
    tagged = [e for e in events if e.get("turn_id") and e.get("user") == "u1"]
    assert tagged and tagged[-1]["agent"] == "main" and tagged[-1]["channel"] == "fake"


async def test_state_api_reports_everything(web_app):
    t = web_app.ctx.tasks.create("job", "g", agent=None, address=None, user_id="alice")
    web_app.ctx.pairing.request("discord", "42", "Zed")
    async with httpx.AsyncClient() as h:
        st = (await h.get(web_app._base + "/api/state", params={"token": "secret"})).json()
    assert st["channels"] == ["fake"] and "main" in st["agents"]
    assert any(x["id"] == t.id for x in st["tasks"])
    assert any(p["sender_id"] == "42" for p in st["pairing"]["pending"])


async def test_stream_delivers_live_events(web_app):
    async with httpx.AsyncClient(timeout=10) as h:
        async def reader():
            async with h.stream("GET", web_app._base + "/api/stream", params={"token": "secret"}) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        return json.loads(line[6:])
            return None

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.3)  # let the subscription register
        await web_app.gateway.channels["fake"].say("trigger a log line")
        event = await asyncio.wait_for(task, timeout=6)
        assert event and "message" in event and "level" in event


async def test_stream_requires_token(web_app):
    async with httpx.AsyncClient() as h:
        r = await h.get(web_app._base + "/api/stream")  # no token
        assert r.status_code == 401


async def test_unauthenticated_when_no_token(tmp_path):
    port = _free_port()
    with pytest.warns(UserWarning, match="UNAUTHENTICATED"):
        cfg = make_config(tmp_path, web_ui={"enabled": True, "host": "127.0.0.1", "port": port})
    app = App(cfg)
    await app.gateway.start()
    await app.web.start()
    try:
        async with httpx.AsyncClient() as h:
            assert (await h.get(f"http://127.0.0.1:{port}/api/state")).status_code == 200  # open
    finally:
        await app.web.stop()
        await app.gateway.stop()
        app.ctx.db.close()
        root = logging.getLogger()
        for hd in [hd for hd in root.handlers if getattr(hd, "_kiro", False)]:
            root.removeHandler(hd)


async def test_state_api_describes_every_agent_for_the_dashboard(web_app):
    web_app.ctx.config.agents["helper"].isolated = True
    web_app.ctx.config.routes.append(
        __import__("kiro_crew.core.config", fromlist=["RouteRule"]).RouteRule(agent="helper", channel="fake", group=True))
    async with httpx.AsyncClient() as h:
        st = (await h.get(web_app._base + "/api/state", params={"token": "secret"})).json()
    rows = {a["name"]: a for a in st["agent_details"]}
    assert set(rows) == {"main", "helper"}
    assert rows["main"]["source"] == "yaml" and "entry_agent" in rows["main"]["routed_from"]
    assert rows["helper"]["isolated"] and rows["helper"]["core_memory"] is False
    assert any(r.startswith("route[0]") for r in rows["helper"]["routed_from"])
    assert rows["main"]["delegates"] == ["helper"] and rows["helper"]["delegate_of"] == ["main"]
    assert st["routes"] == [{"agent": "helper", "channel": "fake", "group": True}]
    assert "system_prompt" not in json.dumps(st["agent_details"])  # prompts are never exposed by the dashboard
