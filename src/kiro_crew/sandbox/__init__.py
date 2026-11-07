"""Isolated command execution for agent tools (``run_shell`` / ``run_python``).

Backends
  docker  every command runs in a throw-away container: no network (default), read-only root, all
          capabilities dropped, non-root, CPU/memory/pids limits, only ``workspace/files`` mounted.
  host    runs on the host with a timeout. UNSAFE; must be chosen explicitly and logs a warning.
  none    sandbox tools refuse to run.

It fails *closed*: if the selected backend is unavailable (e.g. Docker missing) the tools return an error;
they never silently fall back to the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.config import SandboxConfig
from ..core.registry import Registry

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def render(self, limit: int) -> str:
        out = f"exit code: {self.code}" + (" (timed out)" if self.timed_out else "")
        if self.stdout:
            out += f"\n--- stdout ---\n{self.stdout}"
        if self.stderr:
            out += f"\n--- stderr ---\n{self.stderr}"
        return out if len(out) <= limit else out[:limit] + f"\n...[truncated {len(out) - limit} chars]"


class SandboxUnavailable(RuntimeError):
    pass


class SandboxBackend(ABC):
    def __init__(self, cfg: SandboxConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def check(self) -> None:
        """Raise ``SandboxUnavailable`` (with a helpful message) if this backend cannot run."""

    @abstractmethod
    async def run(self, argv: list[str], workdir: Path) -> ExecResult: ...


sandbox_backends: Registry[type[SandboxBackend]] = Registry("sandbox_backend")


async def _communicate(proc: asyncio.subprocess.Process, timeout: int,
                       on_timeout: Callable[[], Awaitable[None]]) -> ExecResult:
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ExecResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
    except TimeoutError:
        await on_timeout()
        with contextlib.suppress(Exception):
            out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
        return ExecResult(124, "", f"killed after {timeout}s", timed_out=True)


@sandbox_backends.register("docker")
class DockerBackend(SandboxBackend):
    def check(self) -> None:
        if shutil.which("docker") is None:
            raise SandboxUnavailable("Docker is not installed or not on PATH; sandbox tools are disabled. "
                                     "Install Docker or set sandbox.backend explicitly.")

    def build_cmd(self, argv: list[str], workdir: Path, name: str) -> list[str]:
        c = self.cfg
        cmd = ["docker", "run", "--rm", "--name", name,
               "--network", "bridge" if c.network else "none",
               "--memory", c.memory, "--cpus", c.cpus, "--pids-limit", "128",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--read-only", "--tmpfs", "/tmp:rw,size=64m",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "-e", "HOME=/tmp",
               "-v", f"{workdir.resolve()}:/work:rw", "-w", "/work",
               c.image, *argv]
        return cmd

    async def run(self, argv: list[str], workdir: Path) -> ExecResult:
        self.check()
        name = f"kiro-crew-{uuid.uuid4().hex[:12]}"
        proc = await asyncio.create_subprocess_exec(
            *self.build_cmd(argv, workdir, name), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def kill() -> None:
            k = await asyncio.create_subprocess_exec("docker", "kill", name, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await k.wait()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

        return await _communicate(proc, self.cfg.timeout_s, kill)


@sandbox_backends.register("host")
class HostBackend(SandboxBackend):
    def check(self) -> None:
        logger.warning("sandbox.backend=host: agent commands run UNSANDBOXED on this machine")

    async def run(self, argv: list[str], workdir: Path) -> ExecResult:
        self.check()
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=workdir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(workdir), "LANG": "C.UTF-8"},
            start_new_session=True)

        async def kill() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

        return await _communicate(proc, self.cfg.timeout_s, kill)


@sandbox_backends.register("none")
class DisabledBackend(SandboxBackend):
    def check(self) -> None:
        raise SandboxUnavailable("Sandbox is disabled (sandbox.backend: none).")

    async def run(self, argv: list[str], workdir: Path) -> ExecResult:
        self.check()
        raise AssertionError("unreachable")


def get_sandbox(cfg: SandboxConfig) -> SandboxBackend:
    return sandbox_backends.get(cfg.backend)(cfg)
