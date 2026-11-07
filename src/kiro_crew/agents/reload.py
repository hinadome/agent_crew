"""Hot-reload agents without restarting: pick up edits to ``agents/*.md`` and the agent-related config.

Safety rules (a typo in a prompt file must never take a running bot down):

* The new configuration is loaded and fully validated *before* anything is applied. On any error the old agents
  keep running and the error is reported.
* A reload is refused if a running channel / task / heartbeat still points at an agent the new config removes.
* Only agent-related settings are applied live: ``agents``, ``routes``, ``mcp_servers``, ``entry_agent``.
  Everything else (models, channels, storage, ...) needs a restart; if it changed, the result says so.
* Cached agents are dropped so the next message uses the new definition. Conversation history is kept (it is
  stored per conversation and agent name).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core import agentfiles
from ..core.config import Config, load_config

if TYPE_CHECKING:
    from ..app import App

logger = logging.getLogger(__name__)

LIVE_FIELDS = {"agents", "routes", "mcp_servers", "entry_agent", "agent_files"}
# Fields where a change cannot take effect without a restart (reported, not applied).
RESTART_FIELDS = ("workspace", "plugins", "models", "memory", "conversation", "tasks", "storage", "logging",
                  "web_ui", "scheduler", "heartbeat", "sandbox", "housekeeping", "tool_options", "approval_timeout_s",
                  "admins", "channels", "identities")


@dataclass
class ReloadResult:
    ok: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    restart_required: list[str] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def noop(self) -> bool:
        return self.ok and not (self.added or self.removed or self.changed)

    def render(self) -> str:
        if not self.ok:
            return f"Reload FAILED, still running the previous agents: {self.error}"
        parts = []
        for label, names in (("added", self.added), ("changed", self.changed), ("removed", self.removed)):
            if names:
                parts.append(f"{label}: {', '.join(names)}")
        text = "Reloaded. " + ("; ".join(parts) if parts else "No agent changes.")
        if self.restart_required:
            text += f" (Restart needed for: {', '.join(self.restart_required)}.)"
        for w in self.warnings:
            text += f"\nwarning: {w}"
        return text


class Reloader:
    def __init__(self, app: App) -> None:
        self.app = app

    def reload(self) -> ReloadResult:
        app = self.app
        if app.config_path is None:
            return ReloadResult(False, error="the app was not started from a config file")
        old = app.cfg
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                new = load_config(app.config_path)
            except Exception as e:  # noqa: BLE001  (report any config problem; never disturb the running bot)
                logger.error("agent reload rejected: %s", e)
                return ReloadResult(False, error=str(e))
        problem = self._refuses(old, new)
        if problem:
            logger.error("agent reload rejected: %s", problem)
            return ReloadResult(False, error=problem)

        result = self._diff(old, new)
        result.warnings = [str(w.message) for w in caught]
        # Apply atomically from the caller's point of view (single-threaded event loop; plain attribute swaps).
        old.agents, old.routes, old.mcp_servers, old.entry_agent = new.agents, new.routes, new.mcp_servers, new.entry_agent
        old.agent_files = new.agent_files
        old._agent_sources = new.agent_sources
        app.gateway.evict()
        app.factory.refresh_mcp()
        logger.info("agents reloaded: %s", result.render())
        return result

    @staticmethod
    def _refuses(old: Config, new: Config) -> str | None:
        """Running parts that name an agent must still resolve against the new definitions."""
        refs: list[tuple[str, str | None]] = [(f"channels.{c}.agent", ch.agent) for c, ch in old.channels.items()
                                              if ch.agent]
        refs += [("tasks.agent", old.tasks.agent), ("heartbeat.agent", old.heartbeat.agent)]
        for where, ref in refs:
            if ref is not None and ref not in new.agents:
                return f"{where} (running) refers to {ref!r}, which the new configuration removes"
        return None

    @staticmethod
    def _diff(old: Config, new: Config) -> ReloadResult:
        r = ReloadResult(True)
        r.added = sorted(set(new.agents) - set(old.agents))
        r.removed = sorted(set(old.agents) - set(new.agents))
        r.changed = sorted(n for n in set(old.agents) & set(new.agents)
                           if old.agents[n].model_dump() != new.agents[n].model_dump())
        od, nd = old.model_dump(by_alias=True), new.model_dump(by_alias=True)
        r.restart_required = [f for f in RESTART_FIELDS if od.get(f) != nd.get(f)]
        return r


def _signature(config_path: Path, agents_dir: Path) -> tuple[Any, ...]:
    out = []
    for p in [config_path, *agentfiles.agent_files(agents_dir)]:
        with contextlib.suppress(OSError):
            st = p.stat()
            out.append((str(p), st.st_mtime_ns, st.st_size))
    return tuple(out)


class AgentWatcher:
    """Polls the config file and the agents folder; reloads (debounced) when something changed."""

    def __init__(self, app: App, reloader: Reloader) -> None:
        self.app, self.reloader = app, reloader
        self._task: asyncio.Task[None] | None = None
        # The baseline is the state the configuration was LOADED from, captured now. Taking it later (when the
        # polling task first runs) would silently swallow any edit made in between.
        self._baseline: tuple[Any, ...] = self._sig() if app.config_path is not None else ()

    def start(self) -> None:
        if self.app.config_path is not None:
            self._task = asyncio.create_task(self._loop(), name="agent-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _sig(self) -> tuple[Any, ...]:
        assert self.app.config_path is not None
        return _signature(self.app.config_path, self.app.cfg.agents_dir)

    async def _loop(self) -> None:
        interval = max(0.2, self.app.cfg.agent_files.interval_s)
        last = self._baseline
        rejected: tuple[Any, ...] | None = None
        while True:
            await asyncio.sleep(interval)
            cur = self._sig()
            if cur == last:
                continue
            await asyncio.sleep(min(0.5, interval))  # debounce: editors often write a file in several steps
            cur = self._sig()
            result = self.reloader.reload()
            if result.ok:
                last, rejected = cur, None
                if not result.noop:
                    logger.info("watcher: %s", result.render())
            elif cur != rejected:  # report a given broken state once, not every poll
                rejected = cur
                last = cur
                logger.error("watcher: %s", result.render())


def write_pidfile(path: Path) -> None:
    path.write_text(str(os.getpid()))


def _looks_like(pid: int, marker: str) -> bool | None:
    """True/False if we can read the process command line (Linux), None if we cannot (then we cannot verify)."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None
    return marker in cmd


def signal_running_process(pidfile: Path, expect: str = "kiro") -> tuple[bool, str]:
    """Ask a running ``kiro-crew run`` to reload by sending SIGHUP (used by ``kiro-crew agent reload``).

    SIGHUP terminates a process that does not handle it, and a stale pid file may name a pid that the OS has since
    reused, so where the command line is readable we refuse to signal anything that does not look like kiro-crew.
    """
    import signal

    try:
        pid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return False, f"no running kiro-crew found (no pid file at {pidfile})"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, f"stale pid file: process {pid} is not running"
    except PermissionError:
        return False, f"process {pid} belongs to another user"
    if _looks_like(pid, expect) is False:
        return False, f"stale pid file: process {pid} is not kiro-crew (refusing to signal it)"
    os.kill(pid, signal.SIGHUP)
    return True, f"asked process {pid} to reload"
