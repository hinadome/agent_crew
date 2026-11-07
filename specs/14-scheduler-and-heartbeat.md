# 14. Scheduler and heartbeat

Source: `scheduler/spec.py`, `scheduler/store.py`, `scheduler/runner.py`, `scheduler/tools.py`.

Purpose: let the assistant act on its own: deliver reminders, run recurring jobs, and periodically check
whether anything needs the user's attention.

## Schedule expressions (`parse_when`)

| Form | Kind | Example | Notes |
|---|---|---|---|
| `in <duration>` | one-shot | `in 30m`, `in 2 hours` | relative to now |
| `at <datetime>` | one-shot | `at 2026-11-07 09:00` | ISO 8601; naive times use `scheduler.timezone`; past times rejected |
| `every <duration>` | recurring | `every 2h` | minimum 60 s |
| `cron <expr>` or bare 5 fields | recurring | `cron 0 9 * * 1-5` | validated by croniter; evaluated in `scheduler.timezone` (DST-aware) |

- **SCH-1** Durations: integer + unit (`s`, `m`, `h`, `d`, `w`, plus long forms). Anything else raises `ScheduleError` with an example.
- **SCH-2** Unrecognised input, intervals under 60 s, past times and invalid cron raise `ScheduleError`; nothing is stored.

## Storage (`schedules` table)

`id`, `kind`, `expr`, `mode`, `title`, `payload`, `agent`, origin address (`channel`, `conversation_id`,
`thread_id`), `user_id`, `enabled`, `next_run`, `last_run`, `run_count`, `quiet_token`, `created_at`.

## Modes

| Mode | On fire | Uses LLM |
|---|---|---|
| `message` | sends `Reminder: <payload>` to the origin chat | no |
| `task` | enqueues a background task with the payload as its goal; result goes to the origin | yes |
| `heartbeat` | enqueues a task built from `HEARTBEAT.md`; a result starting with the OK token is swallowed | yes |

- **SCH-3** Reminders deliberately skip the LLM so they are cheap and reliable.
- **SCH-4** Scheduled tasks reuse the full task machinery (spec 07): own session, retries, timeout, cancel, notification.

## Firing semantics

- **SCH-5** Firing uses a **lease** for **at-least-once** delivery. `claim_due` selects enabled schedules with
  `next_run <= now` whose lease is free (`firing_until` null or in the past), stamps `firing_until = now + lease`
  (default 300 s) **inside one transaction**, and returns them **without advancing `next_run`**. After the firing
  side effect is dispatched, `complete_firing` advances `next_run` (`every`: now + interval; `cron`: next
  occurrence after now; one-shot: disabled), bumps `run_count` and clears the lease. If `fire` raises,
  `release_firing` clears the lease so the next tick retries immediately.
- **SCH-5b** A crash between dispatch and `complete_firing` leaves the lease to expire, after which the schedule is
  re-claimed — a firing is never silently dropped. The trade-off is that such a crash can fire a job **twice**
  (a `message` reminder may repeat; a `task`/`heartbeat` re-runs its agent turn). A still-leased schedule is not
  re-claimed, so concurrent ticks never double-fire.
- **SCH-6** A recurring job missed during downtime fires **once** on restart, then resumes on its normal cadence
  (no backlog storm). A missed one-shot fires late on restart.
- **SCH-7** The loop wakes every `scheduler.tick_s` seconds (default 15) or immediately when a schedule is created.
  An exception while firing one schedule is logged and does not stop the others or the loop.

## Agent tools (tool group `schedule`)

| Tool | Behaviour |
|---|---|
| `remind_me(when, text)` | mode `message` |
| `schedule_job(when, title, instructions)` | mode `task` |
| `list_schedules()` | the caller's active schedules (system schedules hidden) |
| `cancel_schedule(schedule_id)` | only the owner can cancel |

- **SCH-8** Owner, agent and origin come from agent state, so results return to the chat that asked.
- **SCH-9** `scheduler.max_jobs_per_user` (default 50) caps active schedules per owner.
- **SCH-10** Task agents do not receive the `tasks` or `schedule` tools (no recursion).
- Chat commands `/schedules` and `/unschedule <id>`; CLI `kiro-crew schedule list|cancel <id>`.

## Heartbeat

Config: `heartbeat: {enabled, every, target, agent, user, ok_token}`.

- **SCH-11** On start-up, if enabled and `target` is `"<channel>:<conversation_id>"`, a schedule with the fixed id
  `heartbeat` is upserted. If the interval is unchanged the existing `next_run` is kept so restarts do not reset
  the clock. If disabled, the schedule is deleted.
- **SCH-12** `<workspace>/HEARTBEAT.md` is created with a default checklist if missing; the operator edits it.
  Each beat sends the checklist (first 6000 chars) with the instruction: reply exactly the OK token if nothing
  needs attention, otherwise a short actionable message.
- **SCH-13** If the reply starts with the OK token (case-insensitive) the task is marked succeeded and **nothing
  is sent**; otherwise the reply is delivered as `Task [id] done: Heartbeat ...`.
- **SCH-14** The heartbeat runs as the configured memory owner (`heartbeat.user`, default `shared`).

## Limits

- At-least-once means a crash at the wrong moment can repeat a firing; keep scheduled instructions idempotent where it matters.
- Delivery requires the origin channel to be running; otherwise a warning is logged.
- WhatsApp's 24-hour reply window can reject scheduled messages to idle users.
- Scheduling granularity is the tick (default 15 s).
- A scheduled `task` that needs a sensitive tool with `require_approval` asks in the origin chat and is denied after
  the timeout if nobody answers.

## Acceptance criteria *(all verified by the automated suite)*

- Every expression form parses; invalid ones are rejected; cron respects the time zone.
- A due schedule is leased (not advanced) on claim; `complete_firing` advances it; a crash before completion lets the lease expire and the schedule is re-claimed (at-least-once); `release_firing` retries immediately; one-shots disable; missed recurring jobs fire once.
- Users see and cancel only their own schedules; the per-user limit is enforced.
- A running scheduler delivers a due reminder and runs a due task end to end.
- Heartbeat registers once, survives restart without resetting, hides from user lists, stays silent on the OK token,
  and speaks otherwise.
