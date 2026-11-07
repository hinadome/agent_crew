# 07. Background tasks

Source: `tasks/store.py`, `tasks/runner.py`, `tasks/tools.py`.

Purpose: let an agent hand off slow, multi-step work so the chat stays responsive, with durability,
progress, retries and a result delivered to where the request came from.

## Data model (`tasks` table)

| Column | Meaning |
|---|---|
| `id` | 8-hex id |
| `title`, `goal` | label and self-contained instructions |
| `status` | `queued` · `running` · `succeeded` · `failed` · `cancelled` |
| `agent` | agent name that created it (used to run it) |
| `channel`, `conversation_id`, `thread_id` | origin address for notifications (nullable) |
| `user_id` | owner (memory/identity key) |
| `attempts` | incremented at each claim |
| `progress` | JSON list of the latest 50 timestamped notes |
| `result`, `error` | final text / last error |
| `not_before` | earliest time a queued task may be claimed (backoff) |
| `quiet_token` | if the result starts with this (case-insensitive), the task succeeds but **nothing is delivered** (used by heartbeat) |
| `created_at`, `updated_at` | epoch seconds |

The task's agent session id is `task-<id>`.

## Lifecycle

```
queued ──claim──▶ running ──ok──▶ succeeded
   ▲                │ error, attempts < max ─▶ queued (not_before = now + backoff)
   │                │ error, attempts = max ─▶ failed
   └─ restart ──────┘ cancel (queued or running) ─▶ cancelled
```

- **TSK-1** `claim_next()` selects the oldest queued task with `not_before <= now` and sets it to
  `running` (attempts + 1) inside one `BEGIN IMMEDIATE` transaction, so two workers cannot claim the same task.
- **TSK-2** `finish()` never overwrites a terminal status, so a cancel that races completion wins.
- **TSK-3** `requeue()` applies only to `running` tasks.
- **TSK-4** Backoff = `min(30 × 2^(attempts−1), 600)` seconds.

## Execution (`TaskRunner`)

- **TSK-5** `workers` asyncio loops poll for work; they sleep until woken (`wake()`) or 5 s elapse.
- **TSK-6** On `start()`, `recover_interrupted()` sets every `running` task back to `queued`.
- **TSK-7** Each attempt builds a fresh agent via the factory with `session_id = task-<id>`, the task's
  origin and owner, the `TASK_PROMPT` appended to the system prompt, and an extra `report_progress` tool.
  The task agent is not given the `tasks` tool, so tasks cannot spawn tasks.
- **TSK-8** First attempt prompt: `Task: <title>\n\nGoal:\n<goal>`. Later attempts (after a failure or
  restart): an instruction to review prior work in the conversation and continue, plus the last 10 progress notes.
  Because the session is persisted, the agent resumes from its own history.
- **TSK-9** Each attempt is bounded by `task_timeout_s` (`asyncio.wait_for`). A timeout is treated as an
  error (`timed out after <n>s`) and follows the retry rules.
- **TSK-10** Success: store `result` (or `(no output)`), status `succeeded`, notify the origin with
  `Task [<id>] done: <title>` followed by the result.
- **TSK-10b** If `quiet_token` is set and the result starts with it, the result is stored but not sent.
- **TSK-10c** Tasks can also be created by the scheduler (spec 14), with the schedule's origin and owner.
- **TSK-11** Final failure: status `failed`, notify with the attempt count and error.
- **TSK-12** Cancel: `TaskRunner.cancel(id)` marks the row `cancelled` and cancels the asyncio task if
  running; the origin is told `Task [<id>] cancelled.`
- **TSK-13** Shutdown cancels workers and leaves in-flight tasks `running` (not cancelled) so they are
  recovered on the next start.
- **TSK-14** Notification delivery failures are logged and do not change task status.
- **TSK-15** A worker loop that hits an unexpected exception logs it, sleeps 2 s and continues.

## Agent-facing tools (`tasks` tool group)

| Tool | Behaviour |
|---|---|
| `start_task(title, goal)` | creates a task using the calling agent's `origin`, `user_id`, `agent_name` from agent state; wakes workers; returns "Started task <id>" |
| `list_tasks()` | the caller's 15 most recent tasks |
| `task_status(task_id)` | summary, last 10 progress notes, and result if present |
| `cancel_task(task_id)` | cancels via the runner |

Task-agent-only tool: `report_progress(note)` appends a timestamped note (latest 50 kept).

## Interaction with chat

- Results are delivered through `Gateway.send` to the origin address in the same chat/thread.
- `/tasks` and `/cancel` provide the same visibility to the user without invoking the LLM.

## Limits and notes

- If `tasks.enabled` is false, no workers run; tasks created would stay queued.
- Single-process only: starting a second process against the same database would re-queue the first
  process's running tasks at startup.
- WhatsApp may refuse late free-form results (24-hour rule).
- The runner does not push intermediate progress to the chat; progress is visible via status commands.

## Acceptance criteria

- An agent tool call creates a task, a worker completes it, and the origin chat receives the result *(verified)*.
- A task marked `running` after a simulated crash is re-queued and completes *(verified)*.
- `report_progress` persists notes; task agents lack `start_task` *(verified)*.
- Cancelling an unknown id is a no-op returning false *(verified)*.
- Final failure after `max_attempts` marks `failed` and notifies the origin *(verified)*.
- A failed attempt with attempts left is re-queued with a backoff and error recorded *(verified)*; waiting out the backoff and the subsequent resumed run *(unverified; backoff is ≥ 30 s)*.
- A hung attempt is stopped by `task_timeout_s` and recorded as `timed out after <n>s` *(verified)*.
