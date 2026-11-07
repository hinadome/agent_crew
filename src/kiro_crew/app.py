"""Composition root: wires config -> providers, memory, tasks, agents, channels."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from .agents.factory import AgentFactory
from .agents.reload import AgentWatcher, Reloader, signal_running_process, write_pidfile  # noqa: F401
from .channels import channel_class
from .core.config import Config, load_config
from .core.context import AppContext
from .core.logging import configure
from .core.registry import import_plugin_modules
from .gateway import Gateway
from .memory.maintenance import Housekeeper
from .memory.workspace import Workspace
from .providers import ModelFactory
from .scheduler.runner import Scheduler
from .security.approval import ApprovalManager
from .storage import build_storage
from .tasks.runner import TaskRunner
from .web import WebUI

logger = logging.getLogger(__name__)


def configure_logging_from(cfg: Config, *, console: bool = True, force: bool = False) -> None:
    """Install logging from config. If the CLI already configured handlers, its flags win; we still register the
    config's secrets for masking and apply per-area overrides / content policy from config."""
    from .core import logging as klog

    lg = cfg.logging
    secrets = klog.collect_secrets(cfg)
    root = logging.getLogger()
    if not force and any(getattr(h, "_kiro", False) for h in root.handlers):
        klog.set_known_secrets(secrets)
        if lg.include_content and not klog.policy().explicit:
            klog.apply_content_policy(True)
        for area, level in lg.areas.items():
            logging.getLogger(klog._AREA_LOGGERS.get(area, area)).setLevel(level.upper())
        return
    configure(lg.level, areas=lg.areas, file=lg.file, json_format=lg.json_lines, console=console,
              ring_capacity=lg.ring_capacity, strands_level=lg.strands_level, quiet_console=lg.quiet_console,
              include_content=lg.include_content or None, redact_secrets=lg.redact_secrets, secrets=secrets)


def build_context(cfg: Config) -> AppContext:
    import_plugin_modules(cfg.plugins)
    from .tools import register_builtin_tools

    register_builtin_tools()
    ws = Workspace(cfg.workspace)
    storage = build_storage(cfg, ws)
    return AppContext(config=cfg, storage=storage, workspace=ws, models=ModelFactory(cfg.models))


class App:
    def __init__(self, cfg: Config, only_channels: list[str] | None = None,
                 config_path: str | Path | None = None) -> None:
        self.cfg = cfg
        self.config_path: Path | None = Path(config_path).expanduser() if config_path else None
        configure_logging_from(cfg)
        self.ctx = build_context(cfg)
        self.approvals = ApprovalManager(cfg.approval_timeout_s)
        self.ctx.services["approvals"] = self.approvals
        self.factory = AgentFactory(self.ctx)
        self.ctx.services["agent_factory"] = self.factory

        channels = {}
        for name, ccfg in cfg.channels.items():
            if not ccfg.enabled or (only_channels is not None and name not in only_channels):
                continue
            channels[name] = channel_class(name)(name, ccfg)
        self.gateway = Gateway(self.ctx, self.factory, channels)
        self.ctx.services["gateway"] = self.gateway
        self.approvals.send = self.gateway.send
        self.housekeeper = Housekeeper(self.ctx)
        self.scheduler: Scheduler | None = None
        if cfg.scheduler.enabled:
            self.scheduler = Scheduler(self.ctx, self.gateway.send)
            self.ctx.services["scheduler"] = self.scheduler
        self.runner: TaskRunner | None = None
        if cfg.tasks.enabled:
            self.runner = TaskRunner(self.ctx, self.gateway.send)
            self.ctx.services["task_runner"] = self.runner
        self.web: WebUI | None = WebUI(self.ctx) if cfg.web_ui.enabled else None
        self.reloader = Reloader(self)
        self.ctx.services["reload_agents"] = self.reloader.reload
        self.watcher: AgentWatcher | None = (AgentWatcher(self, self.reloader)
                                             if cfg.agent_files.watch and self.config_path else None)

    @property
    def pidfile(self) -> Path:
        return self.cfg.workspace / "kiro-crew.pid"

    async def run(self) -> None:
        if not self.gateway.channels:
            raise SystemExit("No channels enabled. Set `enabled: true` for at least one channel in the config.")
        await self.gateway.start()
        if self.runner:
            self.runner.start()
        if self.scheduler:
            self.scheduler.start()
        self.housekeeper.start()
        if self.web:
            await self.web.start()
        if self.watcher:
            self.watcher.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        with contextlib.suppress(NotImplementedError, AttributeError):  # SIGHUP: reload agents (not on Windows)
            loop.add_signal_handler(signal.SIGHUP, lambda: logger.info(self.reloader.reload().render()))
        # Only advertise ourselves once the SIGHUP handler is installed: SIGHUP's default action is to terminate.
        write_pidfile(self.pidfile)
        quits = [asyncio.create_task(c.quit.wait()) for c in self.gateway.channels.values()]
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait([stopper, *quits], return_when=asyncio.FIRST_COMPLETED)
        logger.info("Shutting down...")
        for t in (stopper, *quits):
            t.cancel()
        if self.watcher:
            await self.watcher.stop()
        if self.web:
            await self.web.stop()
        await self.housekeeper.stop()
        if self.scheduler:
            await self.scheduler.stop()
        if self.runner:
            await self.runner.stop()  # running tasks stay 'running' in the DB and resume on next start
        await self.gateway.stop()
        self.factory.close()  # stop pooled MCP servers
        self.pidfile.unlink(missing_ok=True)
        self.ctx.storage.close()


def load_app(config_path: str | Path, only_channels: list[str] | None = None) -> App:
    return App(load_config(config_path), only_channels, config_path=config_path)
