"""A REAL MCP server over stdio, driven through the app's own agent factory (no mocks of mcp or strands)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.app import App

from ..conftest import make_config

SERVER = str(Path(__file__).with_name("echo_mcp_server.py"))


def _cfg(tmp_path):
    return make_config(
        tmp_path, mcp_servers=[{"name": "calc", "command": sys.executable, "args": [SERVER]}],
        agents={"main": {"mcp_servers": ["calc"]}})


async def test_agent_discovers_and_calls_real_mcp_tools(tmp_path):
    app = App(_cfg(tmp_path))
    agent = app.factory.create(None, session_id=None, user_id="u")
    try:
        # first model call: ask the (scripted) LLM to call the MCP tool; the call really crosses stdio to the server
        await agent.invoke_async('[[tool:calc_add:{"a": 2, "b": 40}]]')
        results = [b["toolResult"] for m in agent.messages for b in m.get("content", []) if "toolResult" in b]
        assert results, f"no tool result recorded; tools were {agent.tool_names}"
        assert results[-1]["status"] == "success"
        assert "42" in json.dumps(results[-1]["content"])
        assert {"calc_add", "calc_shout"} <= set(agent.tool_names)
    finally:
        agent.tool_registry.cleanup()
        app.ctx.storage.close()


async def test_second_mcp_tool_and_unknown_server_config_error(tmp_path):
    app = App(_cfg(tmp_path))
    agent = app.factory.create(None, session_id=None, user_id="u")
    try:
        await agent.invoke_async('[[tool:calc_shout:{"text": "hello mcp"}]]')
        blob = json.dumps(agent.messages)
        assert "HELLO MCP" in blob
    finally:
        agent.tool_registry.cleanup()
        app.ctx.storage.close()
    with pytest.raises(ValueError, match="unknown mcp server"):
        make_config(tmp_path, agents={"main": {"mcp_servers": ["ghost"]}})


def _server_pids():
    """PIDs of running echo MCP servers (argv[1] is the script), excluding this test process."""
    pids = set()
    for p in os.listdir("/proc"):
        if p.isdigit() and int(p) != os.getpid():
            try:
                argv = Path(f"/proc/{p}/cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if len(argv) > 1 and argv[1].endswith(b"echo_mcp_server.py") and b"python" in argv[0]:
                pids.add(int(p))
    return pids


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc to count server processes")
async def test_delegate_calls_share_one_pooled_mcp_server_process(tmp_path):
    """Before pooling, every delegate call spawned a fresh MCP server (measured: 5 calls -> 5 processes)."""
    app = App(make_config(
        tmp_path, mcp_servers=[{"name": "calc", "command": sys.executable, "args": [SERVER]}],
        agents={"main": {"delegates": ["mathy"]}, "mathy": {"description": "does math", "mcp_servers": ["calc"]}}))
    baseline = _server_pids()
    seen: set[int] = set()
    stop = asyncio.Event()

    async def watch():
        while not stop.is_set():
            seen.update(_server_pids() - baseline)
            await asyncio.sleep(0.02)

    watcher = asyncio.create_task(watch())
    try:
        agent = app.factory.create("main", session_id=None, user_id="u")
        for _ in range(5):
            await agent.invoke_async('[[tool:ask_mathy:{"request": "add 1 and 2"}]]')
        # a completely different agent instance (another conversation) reuses the very same server
        await app.factory.create("main", session_id="other", user_id="u2").invoke_async(
            '[[tool:ask_mathy:{"request": "again"}]]')
        await asyncio.sleep(0.3)
        assert len(seen) == 1, f"expected one pooled server process, saw {len(seen)}"
        assert (_server_pids() - baseline) == seen  # still alive between calls (pinned by the pool)
    finally:
        stop.set()
        await watcher
        app.factory.close()  # releases the pool pin and stops the server
    await asyncio.sleep(0.5)
    assert not (_server_pids() - baseline), "pooled MCP server was not stopped on close()"
    app.ctx.storage.close()
