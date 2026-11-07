"""Non-persistent storage backend (``storage.backend: memory``).

Everything lives in this process and vanishes on exit. It exists for demos, throw-away runs and tests, and to
keep the storage abstraction honest: it is an independent implementation of the same protocols as the SQLite
backend, and the shared contract tests run against both.
"""

from __future__ import annotations

import copy
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Collection
from dataclasses import replace
from typing import Any

from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from ..core.config import Config
from ..core.types import Address
from ..memory.workspace import Workspace
from ..scheduler.spec import next_run
from ..scheduler.store import Schedule
from ..security import pairing as P
from ..tasks import store as S
from . import SessionStore, Storage, storage_backends

logger = logging.getLogger(__name__)
_List = list  # the stores below define a method named `list`; annotate with the builtin via this alias


# ---- sessions ----------------------------------------------------------------------------------
class InMemorySessionRepository(SessionStore):
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._session_touched: dict[str, float] = {}
        self._agents: dict[tuple[str, str], SessionAgent] = {}
        self._messages: dict[tuple[str, str], dict[int, tuple[SessionMessage, float]]] = {}
        self._multi: dict[tuple[str, str], dict[str, Any]] = {}

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        with self._lock:
            if session.session_id in self._sessions:
                raise SessionException(f"Session {session.session_id} already exists")
            self._sessions[session.session_id] = copy.deepcopy(session)
            self._session_touched[session.session_id] = time.time()
            return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        with self._lock:
            s = self._sessions.get(session_id)
            return copy.deepcopy(s) if s else None

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._session_touched.pop(session_id, None)
            for store in (self._agents, self._messages, self._multi):
                for k in [k for k in store if k[0] == session_id]:
                    del store[k]

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            doomed = [sid for sid in self._sessions if sid.startswith(prefix)]
            for sid in doomed:
                self.delete_session(sid)
            return len(doomed)

    def prune_idle(self, older_than_s: float, protected: Collection[str] = ()) -> int:
        cutoff = time.time() - older_than_s
        with self._lock:
            doomed = []
            for sid in self._sessions:
                stamps = [ts for (s, _a), msgs in self._messages.items() if s == sid for (_m, ts) in msgs.values()]
                last = max(stamps) if stamps else self._session_touched.get(sid, 0.0)
                if last < cutoff and sid not in set(protected):
                    doomed.append(sid)
            for sid in doomed:
                self.delete_session(sid)
            return len(doomed)

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        with self._lock:
            self._agents[(session_id, session_agent.agent_id)] = copy.deepcopy(session_agent)
            self._messages.setdefault((session_id, session_agent.agent_id), {})

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> SessionAgent | None:
        with self._lock:
            a = self._agents.get((session_id, agent_id))
            return copy.deepcopy(a) if a else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        with self._lock:
            prev = self._agents.get((session_id, session_agent.agent_id))
            if prev is None:
                raise SessionException(f"Agent {session_agent.agent_id} in session {session_id} does not exist")
            session_agent.created_at = prev.created_at
            self._agents[(session_id, session_agent.agent_id)] = copy.deepcopy(session_agent)

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        with self._lock:
            self._messages.setdefault((session_id, agent_id), {})[session_message.message_id] = (
                copy.deepcopy(session_message), time.time())

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kwargs: Any) -> SessionMessage | None:
        with self._lock:
            entry = self._messages.get((session_id, agent_id), {}).get(message_id)
            return copy.deepcopy(entry[0]) if entry else None

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any) -> None:
        with self._lock:
            prev = self.read_message(session_id, agent_id, session_message.message_id)
            if prev is None:
                raise SessionException(f"Message {session_message.message_id} does not exist")
            session_message.created_at = prev.created_at
            self.create_message(session_id, agent_id, session_message)

    def list_messages(self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0,
                      **kwargs: Any) -> list[SessionMessage]:
        with self._lock:
            ordered = [copy.deepcopy(m) for _id, (m, _ts) in sorted(self._messages.get((session_id, agent_id), {}).items())]
            return ordered[offset:] if limit is None else ordered[offset:offset + limit]

    def create_multi_agent(self, session_id: str, multi_agent: Any, **kwargs: Any) -> None:
        with self._lock:
            self._multi[(session_id, multi_agent.id)] = copy.deepcopy(multi_agent.serialize_state())

    def read_multi_agent(self, session_id: str, multi_agent_id: str, **kwargs: Any) -> dict[str, Any] | None:
        with self._lock:
            v = self._multi.get((session_id, multi_agent_id))
            return copy.deepcopy(v) if v is not None else None

    def update_multi_agent(self, session_id: str, multi_agent: Any, **kwargs: Any) -> None:
        with self._lock:
            if (session_id, multi_agent.id) not in self._multi:
                raise SessionException(f"MultiAgent state {multi_agent.id} in session {session_id} does not exist")
            self.create_multi_agent(session_id, multi_agent)


# ---- tasks -------------------------------------------------------------------------------------
class InMemoryTaskStore:
    MAX_PROGRESS = S.TaskStore.MAX_PROGRESS

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, S.Task] = {}
        self._not_before: dict[str, float] = {}

    @staticmethod
    def _copy(t: S.Task) -> S.Task:
        return replace(t, progress=list(t.progress), plan=[dict(p) for p in t.plan], notes=dict(t.notes))

    def create(self, title: str, goal: str, *, agent: str | None, address: Address | None, user_id: str | None,
               quiet_token: str | None = None, done_when: str | None = None,
               parent_id: str | None = None) -> S.Task:
        now = time.time()
        t = S.Task(id=uuid.uuid4().hex[:8], title=title, goal=goal, status=S.QUEUED, agent=agent, address=address,
                   user_id=user_id, attempts=0, created_at=now, updated_at=now, quiet_token=quiet_token,
                   done_when=done_when, parent_id=parent_id)
        with self._lock:
            self._tasks[t.id] = t
            self._not_before[t.id] = 0.0
            return self._copy(t)

    def get(self, task_id: str) -> S.Task | None:
        with self._lock:
            t = self._tasks.get(task_id)
            return self._copy(t) if t else None

    def list(self, *, user_id: str | None = None, active_only: bool = False, limit: int = 20) -> _List[S.Task]:
        with self._lock:
            items = [t for t in self._tasks.values()
                     if (not user_id or t.user_id == user_id) and (not active_only or t.status in S.ACTIVE)]
            items.sort(key=lambda t: t.created_at, reverse=True)
            return [self._copy(t) for t in items[:limit]]

    def claim_next(self) -> S.Task | None:
        now = time.time()
        with self._lock:
            ready = sorted((t for t in self._tasks.values()
                            if t.status == S.QUEUED and self._not_before.get(t.id, 0.0) <= now),
                           key=lambda t: t.created_at)
            if not ready:
                return None
            t = ready[0]
            t.status, t.attempts, t.updated_at = S.RUNNING, t.attempts + 1, now
            return self._copy(t)

    def add_progress(self, task_id: str, note: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.progress = [*t.progress, f"{time.strftime('%H:%M:%S')} {note.strip()}"][-self.MAX_PROGRESS:]
                t.updated_at = time.time()

    def finish(self, task_id: str, status: str, *, result: str | None = None, error: str | None = None) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t and t.status not in S.TERMINAL:  # never overwrite a terminal state (e.g. user cancelled)
                t.status, t.result, t.error, t.updated_at = status, result, error, time.time()

    def requeue(self, task_id: str, *, delay_s: float = 0, error: str | None = None) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t and t.status == S.RUNNING:
                t.status, t.error, t.updated_at = S.QUEUED, error, time.time()
                self._not_before[task_id] = time.time() + delay_s

    def recover_interrupted(self) -> int:
        with self._lock:
            running = [t for t in self._tasks.values() if t.status == S.RUNNING]
            for t in running:
                t.status, t.error, t.updated_at = S.QUEUED, None, time.time()
            return len(running)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if t and t.status in S.ACTIVE:
                t.status, t.updated_at = S.CANCELLED, time.time()
                return True
            return False

    def ask(self, task_id: str, question: str, default: str, deadline: float) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t.status != S.RUNNING:
                return False
            t.status, t.question, t.question_default, t.answer = S.WAITING, question, default, None
            t.wait_kind = "user"
            t.ask_deadline, t.asks, t.updated_at = deadline, t.asks + 1, time.time()
            return True

    def answer(self, task_id: str, text: str, *, kind: str = "user") -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t.status != S.WAITING or t.wait_kind != kind:
                return False
            t.status, t.answer, t.attempts, t.updated_at = S.QUEUED, text, max(t.attempts - 1, 0), time.time()
            self._not_before[task_id] = 0.0
            return True

    def expire_questions(self) -> int:
        now = time.time()
        with self._lock:
            due = [(t.id, t.question_default) for t in self._tasks.values()
                   if t.status == S.WAITING and t.wait_kind == "user" and t.ask_deadline is not None
                   and t.ask_deadline <= now]
        return sum(self.answer(i, S.timeout_answer(d)) for i, d in due)

    def set_plan(self, task_id: str, plan: _List[dict[str, str]]) -> None:
        with self._lock:
            if (t := self._tasks.get(task_id)):
                t.plan, t.updated_at = [dict(p) for p in plan], time.time()

    def set_notes(self, task_id: str, notes: dict[str, str]) -> None:
        with self._lock:
            if (t := self._tasks.get(task_id)):
                t.notes, t.updated_at = dict(notes), time.time()

    def add_tokens(self, task_id: str, n: int) -> None:
        with self._lock:
            if n > 0 and (t := self._tasks.get(task_id)):
                t.tokens += n

    def consume_answer(self, task_id: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.question = t.question_default = t.answer = t.ask_deadline = t.wait_kind = None

    def wait_for_children(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t or t.status != S.RUNNING:
                return False
            t.status, t.wait_kind, t.answer, t.updated_at = S.WAITING, "subtasks", None, time.time()
            return True

    def children(self, parent_id: str) -> _List[S.Task]:
        with self._lock:
            kids = sorted((t for t in self._tasks.values() if t.parent_id == parent_id), key=lambda t: t.created_at)
            return [self._copy(t) for t in kids]

    def ready_parents(self) -> _List[str]:
        with self._lock:
            return [p.id for p in self._tasks.values()
                    if p.status == S.WAITING and p.wait_kind == "subtasks"
                    and not any(c.parent_id == p.id and c.status in S.ACTIVE for c in self._tasks.values())]

    def purge_finished(self, older_than_s: float) -> int:
        cutoff = time.time() - older_than_s
        with self._lock:
            doomed = [i for i, t in self._tasks.items() if t.status in S.TERMINAL and t.updated_at < cutoff]
            for i in doomed:
                del self._tasks[i]
                self._not_before.pop(i, None)
            return len(doomed)

    def active_session_ids(self) -> set[str]:
        with self._lock:
            return {t.session_id for t in self._tasks.values() if t.status in S.ACTIVE}


# ---- schedules ---------------------------------------------------------------------------------
class InMemoryScheduleStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Schedule] = {}
        self._lease: dict[str, float | None] = {}

    def create(self, *, kind: str, expr: str, first_run: float, mode: str, title: str, payload: str,
               agent: str | None, address: Address | None, user_id: str | None,
               quiet_token: str | None = None, id: str | None = None) -> Schedule:
        from ..scheduler.store import MODES

        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        sid = id or uuid.uuid4().hex[:8]
        s = Schedule(sid, kind, expr, mode, title, payload, agent, address, user_id, True, first_run, None, 0,
                     quiet_token)
        with self._lock:
            self._items[sid] = s
            self._lease[sid] = None
            return replace(s)

    def get(self, sid: str) -> Schedule | None:
        with self._lock:
            s = self._items.get(sid)
            return replace(s) if s else None

    def list(self, user_id: str | None = None, include_system: bool = False) -> _List[Schedule]:
        with self._lock:
            items = [s for s in self._items.values() if s.enabled and (not user_id or s.user_id == user_id)
                     and (include_system or s.id != "heartbeat")]
            items.sort(key=lambda s: (s.next_run is None, s.next_run or 0.0))
            return [replace(s) for s in items]

    def count(self, user_id: str | None) -> int:
        with self._lock:
            return sum(1 for s in self._items.values() if s.enabled and s.user_id == user_id)

    def delete(self, sid: str, user_id: str | None = None) -> bool:
        with self._lock:
            s = self._items.get(sid)
            if s is None or (user_id and s.user_id != user_id):
                return False
            del self._items[sid]
            self._lease.pop(sid, None)
            return True

    def claim_due(self, tz: str, now: float | None = None, lease_s: float = 300) -> _List[Schedule]:
        now = time.time() if now is None else now
        with self._lock:
            due = sorted((s for s in self._items.values()
                          if s.enabled and s.next_run is not None and s.next_run <= now
                          and (self._lease.get(s.id) is None or (self._lease[s.id] or 0.0) <= now)),
                         key=lambda s: s.next_run or 0.0)
            for s in due:
                self._lease[s.id] = now + lease_s
            return [replace(s) for s in due]

    def complete_firing(self, sid: str, tz: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            s = self._items.get(sid)
            if s is None:
                return
            nxt = next_run(s.kind, s.expr, tz, now)
            s.last_run, s.run_count, s.next_run, s.enabled = now, s.run_count + 1, nxt, nxt is not None
            self._lease[sid] = None

    def release_firing(self, sid: str) -> None:
        with self._lock:
            if sid in self._lease:
                self._lease[sid] = None


# ---- pairing -----------------------------------------------------------------------------------
class InMemoryPairingStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requests: dict[tuple[str, str], dict[str, Any]] = {}
        self._approved: dict[tuple[str, str], float] = {}

    def is_approved(self, channel: str, sender_id: str) -> bool:
        with self._lock:
            return (channel, sender_id) in self._approved

    def request(self, channel: str, sender_id: str, display_name: str | None = None) -> tuple[str, bool] | None:
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._requests.items() if v["expires_at"] < now]:
                del self._requests[k]
            existing = self._requests.get((channel, sender_id))
            if existing:
                notify = now - existing["last_notified"] >= P.NOTIFY_EVERY_S
                if notify:
                    existing["last_notified"] = now
                return str(existing["code"]), notify
            if len(self._requests) >= P.MAX_PENDING:
                return None
            code = "".join(secrets.choice(P.ALPHABET) for _ in range(P.CODE_LEN))
            self._requests[(channel, sender_id)] = {"code": code, "name": display_name, "expires_at": now + P.TTL_S,
                                                    "last_notified": now, "created_at": now}
            return code, True

    def approve(self, code: str, approved_by: str = "admin") -> P.PairingRequest | None:
        code = code.strip().upper()
        now = time.time()
        with self._lock:
            for (channel, sender), v in list(self._requests.items()):
                if v["code"] == code and v["expires_at"] >= now:
                    del self._requests[(channel, sender)]
                    self._approved[(channel, sender)] = now
                    return P.PairingRequest(channel, sender, code, v["name"], v["expires_at"])
            return None

    def revoke(self, channel: str, sender_id: str) -> bool:
        with self._lock:
            return self._approved.pop((channel, sender_id), None) is not None

    def pending(self) -> _List[P.PairingRequest]:
        now = time.time()
        with self._lock:
            items = sorted(self._requests.items(), key=lambda kv: kv[1]["created_at"])
            return [P.PairingRequest(c, s, v["code"], v["name"], v["expires_at"])
                    for (c, s), v in items if v["expires_at"] >= now]

    def approved(self) -> _List[tuple[str, str]]:
        with self._lock:
            return [k for k, _ts in sorted(self._approved.items(), key=lambda kv: kv[1])]


# ---- settings ----------------------------------------------------------------------------------
class InMemorySettings:
    def __init__(self) -> None:
        self._models: dict[str, str] = {}
        self._agents: dict[str, str] = {}

    def get_model_profile(self, session_key: str) -> str | None:
        return self._models.get(session_key)

    def set_model_profile(self, session_key: str, profile: str) -> None:
        self._models[session_key] = profile

    def get_agent(self, session_key: str) -> str | None:
        return self._agents.get(session_key)

    def set_agent(self, session_key: str, agent: str | None) -> None:
        if agent is None:
            self._agents.pop(session_key, None)
        else:
            self._agents[session_key] = agent


@storage_backends.register("memory")
def build_memory(cfg: Config, workspace: Workspace) -> Storage:
    logger.warning("storage.backend=memory: sessions, tasks, schedules and pairings are NOT persisted")
    return Storage(sessions=InMemorySessionRepository(), tasks=InMemoryTaskStore(), schedules=InMemoryScheduleStore(),
                   pairing=InMemoryPairingStore(), settings=InMemorySettings(), db=None, persistent=False)
