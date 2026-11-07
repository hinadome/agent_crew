# 21. Autonomy: completion verification and guard rails

Source: `tasks/verify.py`, `tasks/runner.py` (`_work`, `_prompt`, `_resume_parents`), `tasks/tools.py` (`start_task`, `ask_user`),
`tasks/state.py`, `tasks/subtasks.py`, `agents/budget.py`,
`agents/factory.py` (`_hooks`), `gateway.py`, `core/config.py` (`TasksConfig`, `LimitsConfig`).

The inner agent loop (model -> tool calls -> model ... until a reply with no tool call) belongs to Strands. This spec covers
what kiro-crew adds around it so unattended runs finish *correctly* and *within bounds*.

## 1. Completion criteria and verification

- `start_task(title, goal, done_when="")`: `done_when` is optional, concrete, checkable text. Stored on the task
  (`tasks.done_when`, nullable; older databases are upgraded in place).
- The worker prompt includes the criteria, and the task system prompt says the result will be checked against them.
- After the agent returns, if `tasks.verify` is on and the task has criteria (or `tasks.verify_all` is on), `verify()` makes one
  tool-less model call (`models.aux` profile, else the default) that sees only the goal, criteria and the result text
  (truncated at 12,000 chars) and must answer JSON `{"met": bool, "feedback": str}`.
- `met: true` -> deliver. `met: false` -> the **same agent session** gets a fix prompt with the criteria and feedback, then the result is
  verified again; at most `tasks.max_verify_rounds` fix rounds (default 2). Rounds happen inside one attempt and share the
  attempt timeout and the tool budget.
- Never lose work: after the last round the result is delivered with `WARNING - not confirmed complete ... Missing: ...`; if the
  reviewer errors or returns no usable JSON the result is delivered with `(Completion could not be verified.)`. The task status is
  `succeeded` in all these cases; the verification notes are in the task progress.
- Heartbeat tasks (`quiet_token`) are never verified.

Limits of this design: the reviewer sees only the final text, not the files or side effects the agent claims to have produced, so
it checks consistency with the criteria, not ground truth. Make criteria observable in the reply ("list the file path and
the first 3 lines").

## 2. Tool-call budget and stuck-loop detection

`BudgetHook` is a Strands hook on every agent (`BeforeToolCallEvent`).

| Rule | Default | Effect |
|---|---|---|
| Same tool with identical arguments more than `limits.repeat_limit` times in a row | 3 | call cancelled, model told to change approach |
| Tool calls per chat turn above `limits.chat_tool_calls` | 40 | call cancelled, model told to answer now |
| Tool calls per task run above `limits.task_tool_calls` | 200 | same (the budget spans the whole run incl. fix rounds, and is not reset between them) |
| More than `limits.grace_calls` blocked calls | 3 | `BudgetExceeded` aborts the run |

Chat turns reset the counters on every invocation (agents are cached per conversation). On a hard stop a chat user receives
`I had to stop: ...`; a task is marked `failed` with the reason and is **not retried** (a retry would burn the same budget). Delegates
get their own hook and counters. `0` disables a limit.

Strands wraps hook exceptions in `EventLoopException`; `find_budget_error` unwraps the chain.

## 3. Asking the user (WAITING)

A task may pause to ask one clear question when a wrong guess would waste significant work.

```
RUNNING --ask_user--> WAITING --/answer or timeout--> QUEUED --claim--> RUNNING (resumes with the answer)
```

- Tool `ask_user(question, default_assumption)` (per task, `tasks/tools.py`). It stores the question, the default and a
  deadline (`tasks.ask_timeout_s`, default 1800 s), sets the status to `waiting`, sends the question to the origin chat and
  tells the model to reply with one line and stop. The runner sees `waiting` after the agent returns and parks the task:
  no result is stored, no "done" message and no verification. The worker is free for other tasks, and the task timeout does
  not tick while it waits.
- Answering: `/answer <id> <text>` in chat (owner only) or `kiro-crew task answer <id> <text>` (works on the database; the running bot
  notices within its 5 s poll). The task goes back to `queued`, the attempt counter is rolled back (an answer is not a retry),
  and the resume prompt contains the question and the answer. The answer is cleared from the row once handed over; the agent's
  saved conversation keeps it.
- No answer in time: the worker loop's `expire_questions()` answers with the agent's own default ("did not answer in time ... proceed
  with your stated default ... mention this in the result").
- Limits: `tasks.max_questions` per task (the tool then tells the model to decide itself); no chat address (scheduled/heartbeat tasks)
  means nobody is asked and the model assumes its default; `tasks.ask_user: false` restores "do not ask questions".
- Waiting tasks survive restarts (recovery only touches `running`), are protected from session pruning, show as `WAITING ... asks:
  <question>` in `/tasks`, `kiro-crew task list` and the dashboard, and can be cancelled.
- After `ask_user` the agent is flagged as paused (`agent.state["paused_for_user"]`): `BudgetHook` cancels every further tool call
  with "this task is paused, reply and stop", and after 3 such calls aborts the run (`TaskPaused`). The runner treats that abort as a
  normal park, not a failure. The same applies after `wait_for_subtasks`.

## 4. Smarter retries

The retry prompt now depends on why the last attempt ended (`TaskRunner._prompt`):

| Last attempt | Retry prompt |
|---|---|
| none (first run) | task + goal + criteria |
| answer pending | the question, the answer, "continue" |
| error | the error text, "do not repeat the same approach; state what you will do differently", then continue |
| timeout | "work more efficiently, skip optional steps, deliver a partial result marked partial" |
| crash / restart | "you were interrupted, continue" (recovery clears the stored error so this is not mistaken for a failure) |

Each error/timeout retry also adds a progress note (`attempt N: retrying after: ...`). Attempts and backoff are unchanged, and
budget stops are still never retried. This cannot make a weak model smarter; it only stops the retry from being blind.

## 5. Plan and notes (structured task state)

Task agents get `update_plan(steps)`, `mark_step(number, status, note)` and `save_note(key, value)` (`tasks/state.py`). Both live in the task
row (`plan` JSON `[{step,status,note}]`, `notes` JSON object) and therefore survive crashes, retries and summarisation.

- `TaskStateHook` (on `BeforeModelCallEvent`) sets the agent's system prompt to *base prompt + `<task_state>`* before **every** model call,
  read fresh from the store. The model sees the current plan even after old messages were summarised away, and a resumed attempt starts with it.
- `update_plan` keeps the status/note of steps whose text is unchanged. Statuses: pending, doing, done, skipped, blocked; `done`/`blocked`
  also write a progress note. Limits: 30 steps × 300 chars, 40 notes, 2000 chars each, 8000 chars in total. An empty value deletes a note.
- Visible as `plan 2/5` in `/tasks`, `task_status` (full plan) and the dashboard. Chat agents do not get these tools or the state block.

## 6. Sub-tasks

Top-level task agents get `start_subtask(title, goal, done_when="")` and `wait_for_subtasks()` (`tasks/subtasks.py`), unless
`tasks.max_subtasks` is 0.

```
parent RUNNING --start_subtask x N--> children QUEUED (parent_id = parent)
parent --wait_for_subtasks--> WAITING (wait_kind=subtasks)          [no worker held]
children run in parallel on free workers; no chat messages, report to the parent
all children terminal --> parent QUEUED with their results as the answer --> resumes
```

- Children: same agent and owner as the parent, no chat address (so they never ask the user and never message the chat), own session,
  own verification (`done_when`), budgets and retries. One level only: sub-tasks get no sub-task tools. At most `tasks.max_subtasks`
  per parent.
- `ready_parents()` is checked by every worker loop iteration, and a finishing child wakes the workers, so the parent resumes promptly and
  also after a restart that happened while children finished.
- Resume prompt: each child's id, title, status and result or error (3000 chars each), then "combine, redo or cover anything that failed,
  and finish". Waiting for children holds no worker, so even `tasks.workers: 1` cannot deadlock.
- `/answer` cannot release a sub-task wait and the question timeout does not apply to it (children have their own timeouts). Cancelling
  a parent cancels its unfinished children; a cancelled or failed child is reported to the parent as such.

## 7. Token budget

`limits.chat_tokens` (per chat turn) and `limits.task_tokens` (per task, summed over all attempts, fix rounds, answers and sub-task
resumes; stored in `tasks.tokens`). Both default to 0 = off, because sensible values depend on model and price.

- Counted from `agent.event_loop_metrics.accumulated_usage.totalTokens` (input + output as the provider reports it). The verifier's own
  calls and sub-tasks are not added to the parent (each sub-task has its own budget).
- At 100 %: tool calls are cancelled with "token budget used up, reply now". Above 125 %: `BudgetExceeded` before the next model call;
  chat users get "I had to stop", the task fails without retry.
- Used tokens show in `/tasks` (`12.3k tokens`) and on the dashboard. No money amounts: use your provider's pricing tools for that.

## 8. Not covered (yet)

Money budgets, dependencies between unrelated tasks, nested sub-tasks, learning from outcomes, tool permission scopes per task.
The token budget is checked between model calls, so one call can overshoot it.

## 9. Tests

`tests/test_autonomy.py`: hook rules (unit), runaway chat turn and runaway task (end to end), per-turn reset on cached agents,
verdict parsing, fix-and-redeliver, bounded rounds with warning, inconclusive/failed reviewer, no-criteria, `verify_all`, switched off,
`done_when` set via the tool. `tests/test_task_questions.py`: ask/wait/answer end to end with a single worker, timeout default, owner-only `/answer`, cancel while waiting, question limit, no-chat case, prompt wording, retry prompts per failure kind. Storage contract (both backends): `done_when`, ask/answer/expire/consume, attempt rollback, recovery clearing errors, plan/notes, sub-task waits and `ready_parents`, tokens. CLI: `kiro-crew task list|answer`. `tests/test_task_state.py`: plan/note tools and limits, state in the system prompt of every call and after a resume. `tests/test_subtasks.py`: one-worker fan-out end to end, parallelism with two workers, failed/cancelled children, cascade cancel, limits, one level, restart catch-up. `tests/test_autonomy.py`: token soft/hard limits, per-turn vs per-task counting, tokens recorded across attempts, paused-tool blocking.
