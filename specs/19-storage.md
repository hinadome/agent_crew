# 19. Storage backends

Source: `storage/__init__.py` (protocols + registry), `storage/sqlite.py`, `storage/inmemory.py`,
`memory/inmem.py`, `memory/maintenance.py`.

Everything the app persists goes through five small interfaces bundled as a `Storage`. Which implementation is
used is a config choice, and new ones plug in through a registry — the same pattern as providers and channels.

## The bundle

| Member | Protocol | Holds |
|---|---|---|
| `sessions` | `SessionStore` (Strands `SessionRepository` + `delete_session`, `prune_idle`) | conversation history |
| `tasks` | `TaskStoreAPI` | the durable background-task queue |
| `schedules` | `ScheduleStoreAPI` | reminders, recurring jobs, heartbeat, with firing leases |
| `pairing` | `PairingStoreAPI` | pending requests and approvals |
| `settings` | `SettingsStore` | per-conversation model profile and agent override |
| `db` | `Database \| None` | the SQL database, only on SQL backends |

`AppContext.storage` is the bundle; `ctx.tasks`, `ctx.schedules`, `ctx.pairing`, `ctx.settings` are shortcuts.
`ctx.db` raises a clear error on backends without SQL.

## Selecting a backend

```yaml
storage:
  backend: sqlite     # default; one local file in the workspace
  # backend: memory   # nothing is persisted
```

- **STO-1** `build_storage` resolves `storage.backend` in the `storage_backends` registry
  (lazy-importing the built-ins). Unknown names fail at startup listing the available ones.
- **STO-2** A backend is `(Config, Workspace) -> Storage`:

  ```python
  from kiro_crew.storage import Storage, storage_backends

  @storage_backends.register("postgres")
  def build(cfg, workspace) -> Storage: ...
  ```
  Entry-point group: `kiro_crew.storage_backends`.
- **STO-3** `Storage.close()` runs the backend's closers; the app and the admin CLI call it on exit.

## Built-in backends

| Backend | Persistence | Notes |
|---|---|---|
| `sqlite` | one file, WAL | the only backend supporting `memory.backend: sqlite` / `hybrid` (they need `db`) |
| `memory` | none (process lifetime) | logs a WARNING at start; use `memory.backend: inmem` for facts; for demos and tests |

- **STO-4** SQL-only memory backends (`sqlite`, `hybrid`) on a non-SQL storage fail with
  `... needs the 'sqlite' storage backend` rather than an `AttributeError`.
- **STO-5** `memory.backend: inmem` keeps facts in the `Storage` object (`extras`) so every store instance for a user
  sees the same data; it supports the same admin API as the SQLite store (`list`, `count`, `delete`, `clear`), so
  `/memory` works on it. Ranking is word-overlap, not BM25.

## Behavioural contract

The protocols fix the *behaviour*, not just the signatures. Highlights every backend MUST honour:

- **Tasks:** FIFO atomic claim (a task is never returned twice); `attempts` increments on claim; `requeue` applies only
  to running tasks and honours a delay; `finish` never overwrites a terminal state; `recover_interrupted` re-queues running
  tasks; progress capped at 50 timestamped notes; `purge_finished` removes only old terminal tasks;
  `active_session_ids` lists queued/running tasks' sessions.
- **Schedules:** `claim_due` leases without advancing; a leased schedule is not re-claimed until the lease expires
  (crash recovery); `release_firing` allows immediate retry; `complete_firing` advances (one-shots disable); a missed
  recurring job fires once; `create` with an existing id replaces it; the `heartbeat` system schedule is hidden from user
  lists; cancel is owner-scoped.
- **Pairing:** codes are 8 chars, case-insensitive, single-use, expire after 1 hour, re-shown (not re-issued) with a
  10-minute notification throttle, and at most 20 are pending.
- **Sessions:** `create_session` rejects duplicates; `update_*` preserve `created_at` and reject missing rows; messages
  list in id order with pagination; reads return **isolated copies**; `delete_session` removes everything and is
  idempotent; `prune_idle` honours age and the protected set.
- **Settings:** last write wins; unknown keys return `None`; the model profile and the agent override are independent (setting one never clobbers the other); `set_agent(key, None)` clears the override.
- **Sessions (prefix):** `delete_prefix` matches the prefix **literally** (`_` and `%` are not wildcards), removes all matching sessions, and leaves the parent and look-alikes alone. It is how `/new` removes a conversation's stateful-delegate sessions.

These are encoded in `tests/test_storage_contract.py`, parametrised over every backend. **Run it against any backend you
write** (add it to `BACKENDS` or provide your own fixture).

## Housekeeping is storage-agnostic

`memory/maintenance.py` contains no SQL: it calls `tasks.purge_finished`, `tasks.active_session_ids` and
`sessions.prune_idle`, so retention works on any backend.

## Acceptance criteria *(all verified by the automated suite)*

- Every contract test passes on both `sqlite` and `memory`.
- The contract detects a broken backend independently: deliberately breaking the memory backend's terminal-state guard,
  lease, or pairing single-use fails only the `[memory]` cases; breaking the SQLite guard fails only `[sqlite]`
  (fault injection).
- The full app (gateway, agents, tasks with notification and crash recovery, scheduler reminders, pairing,
  `/model`, `/new`, `/memory`, housekeeping) runs on the `memory` backend with no SQLite file created.
- mypy accepts both backends as `Storage` members (structural protocol check).
- A real Postgres/DynamoDB backend *(not provided)*.
