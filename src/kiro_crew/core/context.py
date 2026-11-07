"""Dependency container passed to tool factories, agents, tasks and the gateway."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from ..memory.db import Database
    from ..memory.workspace import Workspace
    from ..providers import ModelFactory
    from ..storage import PairingStoreAPI, ScheduleStoreAPI, SettingsStore, Storage, TaskStoreAPI


@dataclass
class AppContext:
    config: Config
    storage: Storage
    workspace: Workspace
    models: ModelFactory
    # Late-bound services (set by the app once built) to avoid import cycles.
    services: dict[str, Any] = field(default_factory=dict)
    #: Set on the per-agent view returned by :meth:`for_agent`; tool factories use it to apply isolation.
    agent_name: str | None = None

    def for_agent(self, name: str) -> AppContext:
        """A view of this context for one agent (shares all services/stores; knows which agent is asking)."""
        return replace(self, agent_name=name)

    @property
    def files_root(self) -> Path:
        """The sandbox folder this agent's file/command tools operate in (shared, or private if isolated)."""
        if self.agent_name is not None and self.config.agents[self.agent_name].private_files:
            return self.workspace.agent_files_dir(self.agent_name)
        return self.workspace.files_dir

    # Convenience accessors onto the storage bundle (kept so call sites read naturally).
    @property
    def tasks(self) -> TaskStoreAPI:
        return self.storage.tasks

    @property
    def schedules(self) -> ScheduleStoreAPI:
        return self.storage.schedules

    @property
    def pairing(self) -> PairingStoreAPI:
        return self.storage.pairing

    @property
    def settings(self) -> SettingsStore:
        return self.storage.settings

    @property
    def db(self) -> Database:
        """The SQL database. Only SQL storage backends have one; others raise a clear error."""
        if self.storage.db is None:
            raise ValueError("this storage backend has no SQL database (use storage.backend: sqlite)")
        return self.storage.db
