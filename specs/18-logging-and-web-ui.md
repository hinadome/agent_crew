# 18. Logging and the web dashboard

Source: `core/logging.py`, `agents/trace.py`, `web/__init__.py`, plus DEBUG calls across the gateway,
tasks, scheduler and tools.

Goal: full, structured visibility into what the process is doing, both in the terminal/a file and live in
a browser, correlated per conversation turn.

## Log model

- **LOG-1** One `configure()` installs handlers on the root logger: a console handler, an optional
  rotating file handler, and an in-memory **ring buffer** that feeds the web UI. It is idempotent and only
  replaces handlers it previously added (foreign handlers are left alone).
- **LOG-2** Levels: a **base** level for `kiro_crew.*`, plus **per-area overrides**. Areas map friendly
  names to logger prefixes: `gateway`, `agents`, `tasks`, `scheduler`, `memory`, `tools`, `channels`,
  `security`, `web_ui`, `strands` (or any raw logger name).
- **LOG-3** Handlers are set to the **minimum** of the base and all area levels, so a per-area DEBUG
  override actually emits even when the base is INFO.
- **LOG-4** Strands' own logger defaults to WARNING (so it does not drown the app) unless the base is DEBUG
  or `strands_level` is set.
- **LOG-5** Output format is human text by default or **JSON lines** (`json: true` / `--log-json`), one
  object per record, suitable for ingestion.
- **LOG-6** `quiet_console: true` keeps the console at INFO while the file and web UI still capture DEBUG —
  useful for `kiro-crew chat`, where a readable terminal matters.

## Per-turn correlation

- **LOG-7** Each unit of work sets a `TurnContext` (via `contextvars`) carrying a generated `turn_id` and
  `kind` (`chat`, `task`, `schedule`, `heartbeat`, `delegate`), plus `channel`, `conversation`, `user`,
  `agent`. Every log record emitted during that work inherits these, so one conversation can be followed
  across the gateway, agent, tools, memory and tasks by filtering on `turn_id`.
- **LOG-8** Turn context is attached by: the gateway (per inbound message), the task runner (per task
  attempt), and the scheduler (per fire).

## Structured fields

- **LOG-9** `log(logger, level, msg, **fields)` is the structured helper. Fields appear as `k=v` on the
  console line and as a `fields` object in JSON and the web UI. It is a no-op when the level is disabled, so
  DEBUG tracing costs nothing when off.
- **LOG-10** Field names that collide with reserved `LogRecord` attributes (e.g. `name`, `module`, `args`)
  are renamed with an `f_` prefix rather than crashing logging. (The gateway passes `name=<sender>`; this is
  why the guard exists.)

## Redaction (safe to enable)

Verified empirically with canary strings: before this work, a chat message and the dashboard token both appeared in
the console log and in the dashboard's own feed.

- **LOG-13** **Content hiding (default ON).** Fields named `text`, `args`, `result`, `argv`, `command`, `code`, `content`,
  `prompt`, `body`, `output`, `stdout`, `stderr`, `query` are replaced by `<N chars hidden>`. Metadata stays visible
  (sender, agent, tool names, durations, sizes, statuses), so the flow is still observable. Opt in with
  `logging.include_content: true` or `--log-content`.
- **LOG-14** **Secret scrubbing (default ON, independent of content hiding).** A handler filter on the console, file and
  dashboard sinks masks, in the formatted message, every string extra and exception tracebacks: known token shapes
  (OpenAI/Anthropic `sk-`, AWS `AKIA`/`ASIA`, Slack `xox*`/`xapp-`, Discord, Telegram, GitHub, JWT, `Bearer ...`),
  `key=value`/`"key": "value"` credentials (`api_key`, `secret`, `token`, `password`, `authorization`, ...), and the
  **exact secret values found in the config** (channel tokens, API keys, the web token, MCP headers). Applies to every
  logger, so third-party and future log calls are covered.
- **LOG-15** **Third-party DEBUG is capped.** Strands logs the memory-search query and the whole `tool_use` dict at DEBUG;
  httpx/openai/anthropic/litellm/botocore/... can log bodies or headers. While content hiding is on, these loggers are held
  at WARNING even if a higher verbosity was requested; `include_content` lifts the cap. A one-time INFO note says so.
- **LOG-16** The dashboard disables aiohttp's access log (its request line carries `?token=`); the scrubber and the
  third-party cap are further layers.
- **LOG-17** Limits: scrubbing is pattern-based. A credential with no recognisable shape that is not in the config can
  still appear; and with `include_content` on, content is logged verbatim (secrets in it are still masked by shape).

## What is traced at DEBUG

| Area | Logger | Events |
|---|---|---|
| gateway | `kiro_crew.gateway` | inbound message (sender, text), auth decision, chosen agent/model, reply (chars, ms), command handled |
| agents | `kiro_crew.agents.trace` | tool call (name, args), tool result (status, ms), model call/return, message added (role, block kinds) |
| tasks | `kiro_crew.tasks` | attempt start, success/quiet/fail, retry/backoff (existing INFO/EXCEPTION) |
| scheduler | `kiro_crew.scheduler` | firing (id, mode), heartbeat registration |
| tools | `kiro_crew.tools.web`, `.sandbox` | `web_fetch` target and block/ok/error, sandbox command, exit code |
| memory | `kiro_crew.memory` | embedding/search degradation (existing WARNING) |

The agent trace is a Strands `HookProvider` (`TraceHook`) attached to every agent, covering
`Before/AfterToolCall`, `Before/AfterModelCall` and `MessageAdded`.

## CLI

```
kiro-crew [-v] [--log-level L] [--log AREA=LEVEL ...] [--log-file PATH] [--log-json] [--log-content] <command>
```

- **LOG-11** `-v` == `--log-level DEBUG`. `--log` is repeatable (`--log gateway=debug --log memory=info`).
- **LOG-12** If any logging flag is given it wins over config and applies to **every** command, including
  the offline admin ones (`pairing`, `memory`, `schedule`, `prune`); otherwise logging is driven by the
  `logging:` config block when the app starts.

## Config

```yaml
logging:
  level: INFO
  areas: {gateway: debug, tools: debug}
  file: ~/.kiro-crew/kiro-crew.log
  json: false
  quiet_console: false
  ring_capacity: 2000
  strands_level: WARNING
  include_content: false     # hide message text / tool args / results (default)
  redact_secrets: true       # mask token shapes + the config's exact secrets in every line
```

Invalid levels are rejected at config load.

## Web dashboard

Source: `web/__init__.py`. Enabled with `web_ui.enabled`; served with aiohttp.

- **WEB-UI-1** Read-only. It never mutates state. Endpoints:
  - `GET /` — single-page dashboard (no build step; inline HTML/JS).
  - `GET /api/logs?limit&level` — recent ring-buffer events as JSON.
  - `GET /api/stream` — Server-Sent Events; each new log event is pushed live (with keep-alives).
  - `GET /api/state` — current channels, agents, recent tasks, schedules (incl. the heartbeat), and pairing
    (pending + approved).
- **WEB-UI-2** **Auth:** every `/api/*` endpoint requires `web_ui.token` via `?token=` or the
  `X-Auth-Token` header; a missing/wrong token returns 401. If no token is configured the dashboard is
  unauthenticated and config load emits a warning.
- **WEB-UI-3** It binds `127.0.0.1` by default. It exposes conversation metadata and message text in logs,
  so put it behind your own TLS/auth before exposing it beyond localhost.
- **WEB-UI-4** The log stream fans out from the ring buffer to each subscriber via an async queue; a slow
  viewer drops events rather than blocking logging. Logging from worker threads is marshalled onto the loop.
- **WEB-UI-5** The page lets you filter by level and free text, follow (tail), and see turns, tasks,
  schedules and pairing refreshed every few seconds.

The dashboard shows the same per-turn tags as the logs, so you can watch a message arrive, see which tools
the agent called, and see the reply, live.

## Limits

- The ring buffer is in-memory (`ring_capacity`, default 2000 events); history beyond that lives only in the
  file sink if configured. There is no query/search API beyond `limit` and `level`.
- The UI is single-node (it reads this process's ring buffer).
- No metrics or tracing export yet (Strands already emits OpenTelemetry spans; wiring them is on the roadmap).

## Acceptance criteria *(verified by the automated suite)*

- `configure` installs a ring, sets base/area/strands levels, and replaces only its own handlers.
- Per-area DEBUG override emits even at an INFO base; `quiet_console` keeps the console at INFO while the
  ring still captures DEBUG; JSON and file sinks work; the ring is capacity-bounded; `recent(min_level)`
  filters.
- Turn context tags records and clears afterwards; reserved field names do not crash logging.
- The DEBUG trace records an inbound message and a reply tagged to the turn, with the tool/model activity in
  between.
- Web UI: token required on all APIs; index served; `/api/logs` and `/api/state` return the expected data;
  `/api/stream` delivers a live event; no token ⇒ open with a load-time warning.
- Redaction (canary test through the real gateway, agent, tools and dashboard): by default the web token, a chat message and a
  tool argument appear nowhere in the console or the dashboard feed; with `include_content` the message and tool argument
  appear but the web token never does; token shapes, key=value forms, config secrets and tracebacks are masked; Strands and
  httpx are capped while content is hidden.
- Disabling web auth, the reserved-name guard, the effective-handler-level logic, content hiding, the third-party cap, the
  traceback scrub, or default secret scrubbing each makes tests fail (fault injection).
- Real browsers, large-volume streaming and non-localhost exposure *(unverified)*.
