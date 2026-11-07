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

## 3. Not covered (yet)

Token or money budgets, parallel sub-tasks, task dependencies, a task asking the user a question and resuming, structured per-task state,
explicit plans, learning from outcomes. A tool-call cap does not bound cost per call (a single huge tool result can still be expensive).

## 4. Tests

`tests/test_autonomy.py`: hook rules (unit), runaway chat turn and runaway task (end to end), per-turn reset on cached agents,
verdict parsing, fix-and-redeliver, bounded rounds with warning, inconclusive/failed reviewer, no-criteria, `verify_all`, switched off,
`done_when` set via the tool. Storage contract: `done_when` round-trips on both backends.
