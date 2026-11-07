"""The command sandbox against a REAL Docker daemon. Skips if Docker is not installed/running.

Optional: KIRO_IT_DOCKER_IMAGE (default python:3.12-slim; must contain sh and python3).
"""

import asyncio
import os

import pytest

from kiro_crew.core.config import SandboxConfig
from kiro_crew.sandbox import DockerBackend

pytestmark = pytest.mark.usefixtures("docker")
IMAGE = os.environ.get("KIRO_IT_DOCKER_IMAGE", "python:3.12-slim")


def _backend(**kw):
    return DockerBackend(SandboxConfig(image=IMAGE, timeout_s=kw.pop("timeout_s", 60), **kw))


async def test_runs_a_command_and_returns_output(tmp_path):
    r = await _backend().run(["sh", "-c", "echo hello; echo oops >&2; exit 3"], tmp_path)
    assert (r.code, r.stdout.strip(), r.stderr.strip(), r.timed_out) == (3, "hello", "oops", False)


async def test_network_is_off_by_default(tmp_path):
    code = "import socket; socket.create_connection(('1.1.1.1', 53), 3)"
    r = await _backend().run(["python3", "-c", code], tmp_path)
    assert r.code != 0 and "Network is unreachable" in (r.stderr + r.stdout) or r.code != 0


async def test_root_filesystem_is_read_only_but_tmp_and_work_are_writable(tmp_path):
    r = await _backend().run(["sh", "-c", "touch /rootfile"], tmp_path)
    assert r.code != 0
    r = await _backend().run(["sh", "-c", "touch /tmp/ok && echo persisted > /work/out.txt"], tmp_path)
    assert r.code == 0
    assert (tmp_path / "out.txt").read_text().strip() == "persisted"  # visible on the host: the shared folder


async def test_runs_as_the_host_user_not_root(tmp_path):
    r = await _backend().run(["id", "-u"], tmp_path)
    assert r.stdout.strip() == str(os.getuid()) and r.stdout.strip() != "0"


async def test_all_capabilities_dropped(tmp_path):
    r = await _backend().run(["sh", "-c", "grep CapEff /proc/self/status"], tmp_path)
    assert r.stdout.split()[-1].strip("0") == ""  # CapEff: 0000000000000000


async def test_timeout_kills_the_container(tmp_path):
    r = await _backend(timeout_s=2).run(["sh", "-c", "sleep 60"], tmp_path)
    assert r.timed_out and r.code == 124
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "name=kiro-crew-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    assert out.decode().strip() == ""  # no orphaned container


async def test_sandbox_tool_end_to_end(tmp_path):
    from kiro_crew.app import App
    from kiro_crew.tools import resolve_tools

    from ..conftest import make_config

    app = App(make_config(tmp_path, sandbox={"backend": "docker", "image": IMAGE, "timeout_s": 60},
                          agents={"main": {"tools": ["sandbox"]}}))
    try:
        tools = {t.tool_name: t for t in resolve_tools(["sandbox"], app.ctx)}
        out = await tools["run_python"]("print(6 * 7)")
        assert "exit code: 0" in out and "42" in out
    finally:
        app.ctx.storage.close()
