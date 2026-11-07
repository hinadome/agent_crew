# 21. Autonomy: completion verification and guard rails

Source: `tasks/verify.py`, `tasks/runner.py` (`_work`), `tasks/tools.py` (`start_task`), `agents/budget.py`,
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
- Relies on the model obeying "stop after asking". If it keeps working anyway, the run still ends in the parked state when it returns, and the
  work it did meanwhile is kept in the session. Further tool calls are not blocked.

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

## 5. Not covered (yet)

Token or money budgets, parallel sub-tasks, task dependencies, structured per-task state,
explicit plans, learning from outcomes. A tool-call cap does not bound cost per call (a single huge tool result can still be expensive).

## 6. Tests

`tests/test_autonomy.py`: hook rules (unit), runaway chat turn and runaway task (end to end), per-turn reset on cached agents,
verdict parsing, fix-and-redeliver, bounded rounds with warning, inconclusive/failed reviewer, no-criteria, `verify_all`, switched off,
`done_when` set via the tool. `tests/test_task_questions.py`: ask/wait/answer end to end with a single worker, timeout default, owner-only `/answer`, cancel while waiting, question limit, no-chat case, prompt wording, retry prompts per failure kind. Storage contract (both backends): `done_when`, ask/answer/expire/consume, attempt rollback, recovery clearing errors. CLI: `kiro-crew task list|answer`.
