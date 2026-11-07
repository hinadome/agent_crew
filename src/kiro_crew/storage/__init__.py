"""Pluggable persistence.

Everything the app persists goes through five small interfaces, bundled as a :class:`Storage`:

  sessions   conversation history (a Strands ``SessionRepository`` + retention)
  tasks      the durable background-task queue
  schedules  reminders / recurring jobs / heartbeat, with firing leases
  pairing    access requests and approvals
  settings   per-conversation settings (the chosen model profile)

A *storage backend* builds that bundle from the config. ``sqlite`` (default) is one local file;
``memory`` is a non-persistent implementation useful for demos and tests. To use Postgres, DynamoDB etc.,
register a backend that implements these protocols::

    from kiro_crew.storage import storage_backends, Storage

    @storage_backends.register("postgres")
    def build(cfg, workspace) -> Storage: ...

The same behavioural contract tests (``tests/test_storage_contract.py``) run against every built-in backend;
run them against yours too.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from strands.session import SessionRepository

from ..core.registry import Registry

if TYPE_CHECKING:
    from ..core.config import Config
    from ..core.types import Address
    from ..memory.db import Database
    from ..memory.workspace import Workspace
    from ..scheduler.store import Schedule
    from ..security.pairing import PairingRequest
    from ..tasks.store import Task


_List = list  # several stores define a method named `list`, which shadows the builtin in annotations


class SessionStore(SessionRepository):
    """A Strands session repository plus the housekeeping operations the app needs."""

    def delete_session(self, session_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> int:  # pragma: no cover - interface
        """Delete every session whose id starts with ``prefix`` (literal match, no wildcards). Returns the count."""
        raise NotImplementedError

    def prune_idle(self, older_than_s: float, protected: Collection[str] = ()) -> int:  # pragma: no cover
        """Delete sessions idle for longer than ``older_than_s``, except ``protected`` ids. Returns the count."""
        raise NotImplementedError


class TaskStoreAPI(Protocol):
    def create(self, title: str, goal: str, *, agent: str | None, address: Address | None,
               user_id: str | None, quiet_token: str | None = None, done_when: str | None = None) -> Task: ...
    def get(self, task_id: str) -> Task | None: ...
    def list(self, *, user_id: str | None = None, active_only: bool = False, limit: int = 20) -> _List[Task]: ...
    def claim_next(self) -> Task | None: ...
    def add_progress(self, task_id: str, note: str) -> None: ...
    def finish(self, task_id: str, status: str, *, result: str | None = None, error: str | None = None) -> None: ...
    def requeue(self, task_id: str, *, delay_s: float = 0, error: str | None = None) -> None: ...
    def recover_interrupted(self) -> int: ...
    def cancel(self, task_id: str) -> bool: ...
    def purge_finished(self, older_than_s: float) -> int: ...
    def active_session_ids(self) -> set[str]: ...


class ScheduleStoreAPI(Protocol):
    def create(self, *, kind: str, expr: str, first_run: float, mode: str, title: str, payload: str,
               agent: str | None, address: Address | None, user_id: str | None,
               quiet_token: str | None = None, id: str | None = None) -> Schedule: ...
    def get(self, sid: str) -> Schedule | None: ...
    def list(self, user_id: str | None = None, include_system: bool = False) -> _List[Schedule]: ...
    def count(self, user_id: str | None) -> int: ...
    def delete(self, sid: str, user_id: str | None = None) -> bool: ...
    def claim_due(self, tz: str, now: float | None = None, lease_s: float = 300) -> _List[Schedule]: ...
    def complete_firing(self, sid: str, tz: str, now: float | None = None) -> None: ...
    def release_firing(self, sid: str) -> None: ...


class PairingStoreAPI(Protocol):
    def is_approved(self, channel: str, sender_id: str) -> bool: ...
    def request(self, channel: str, sender_id: str, display_name: str | None = None) -> tuple[str, bool] | None: ...
    def approve(self, code: str, approved_by: str = "admin") -> PairingRequest | None: ...
    def revoke(self, channel: str, sender_id: str) -> bool: ...
    def pending(self) -> _List[PairingRequest]: ...
    def approved(self) -> _List[tuple[str, str]]: ...


class SettingsStore(Protocol):
    def get_model_profile(self, session_key: str) -> str | None: ...
    def set_model_profile(self, session_key: str, profile: str) -> None: ...
    def get_agent(self, session_key: str) -> str | None: ...
    def set_agent(self, session_key: str, agent: str | None) -> None: ...


@dataclass
class Storage:
    sessions: SessionStore
    tasks: TaskStoreAPI
    schedules: ScheduleStoreAPI
    pairing: PairingStoreAPI
    settings: SettingsStore
    #: Only SQL backends have one. Memory backends that need SQL (``sqlite``, ``hybrid``) check for it.
    db: Database | None = None
    persistent: bool = True
    #: Backend-private shared state (e.g. the in-process fact store of the ``memory`` storage backend).
    extras: dict[str, Any] = field(default_factory=dict)
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for c in self._closers:
            c()


StorageBuilder = Callable[["Config", "Workspace"], Storage]
storage_backends: Registry[StorageBuilder] = Registry("storage_backend")

_BUILTIN = {"sqlite": "kiro_crew.storage.sqlite", "memory": "kiro_crew.storage.inmemory"}


def build_storage(cfg: Config, workspace: Workspace) -> Storage:
    name = cfg.storage.backend
    if name not in storage_backends and name in _BUILTIN:
        importlib.import_module(_BUILTIN[name])
    return storage_backends.get(name)(cfg, workspace)


def require_db(storage_or_db: Any, who: str) -> Database:
    """Helper for SQL-only components: a clear error instead of an AttributeError on ``None``."""
    db = getattr(storage_or_db, "db", storage_or_db)
    if db is None:
        raise ValueError(f"{who} needs the 'sqlite' storage backend (this storage backend has no SQL database)")
    return db
