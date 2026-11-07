# 02. Configuration

Source: `core/config.py`, `default_config.yaml`. Generate a starter file with `kiro-crew init`.

## Loading rules

- **CFG-1** The config is one YAML file, default `./kiro-crew.yaml`, overridable with `-c/--config`.
- **CFG-2** Before validation every string value (recursively, in lists and maps) has environment
  references expanded: `${VAR}` becomes the variable's value or `""` if unset; `${VAR:-fallback}`
  becomes `fallback` when `VAR` is unset. A variable that is set but empty expands to empty.
- **CFG-3** The expanded document is validated by Pydantic models with `extra="forbid"`, so a misspelt
  key is an error. The single exception is `channels.<name>`, which allows extra keys (adapter settings).
- **CFG-4** Validation failure aborts startup with the Pydantic error listing the offending path.
- **CFG-5** `workspace` has `~` expanded and is resolved to an absolute path.

## Cross-reference validation (CFG-6)

Startup MUST fail if:

| Condition | Message subject |
|---|---|
| `models.default`, any `models.fallback` entry or `models.aux` is not a defined profile | unknown profile |
| `scheduler.timezone` is not a valid IANA zone | zone error |
| a channel's `access` is not `allowlist` or `pairing` | access |
| `entry_agent` is not under `agents` | entry_agent not defined |
| an agent's `model` is not a defined profile | unknown model profile |
| an agent's `delegates` entry is not a defined agent, or it delegates to itself | unknown agent / itself |
| an agent name is invalid | invalid, with the naming rule |
| `channels.<n>.agent`, `channels.<n>.agents_allowed`, `routes[i].agent`, `tasks.agent`, `heartbeat.agent` name an unknown agent | refers to unknown agent |
| a `routes` rule has no match field | no match field |
| an agent that cannot see core memory is given the `core_memory` tool | core_memory |
| an agent's `mcp_servers` entry is not in top-level `mcp_servers` | unknown mcp server |

Not validated at load time (checked on use): provider names, tool names, channel names, memory
backend names, `plugins` importability (imported at build time).

## Schema

### Top level

| Key | Type | Default | Notes |
|---|---|---|---|
| `workspace` | path | `~/.kiro-crew` | holds `SOUL.md`, `MEMORY.md`, `files/`, `kiro-crew.db` |
| `plugins` | list[str] | `[]` | module paths imported at startup so they can self-register |
| `entry_agent` | str | `main` | agent used by channels without an `agent:` override |
| `models` | object | required | see below |
| `memory` | object | defaults | |
| `conversation` | object | defaults | |
| `tasks` | object | defaults | |
| `agents` | map | required | at least the entry agent; names `^[A-Za-z][A-Za-z0-9_-]{0,31}$`. Drop-in files in `agent_files.dir` are merged in (spec 20) |
| `agent_files` | object | `{dir: agents, watch: true, interval_s: 3}` | drop-in folder (relative to the workspace) and auto-reload |
| `routes` | list | `[]` | pick the agent per message: `{agent, channel?, conversation?, user?, group?}`; first match wins |
| `storage` | object | `sqlite` | persistence backend, see below and spec 19 |
| `logging` | object | defaults | levels, sinks, redaction (spec 18) |
| `web_ui` | object | disabled | read-only dashboard (spec 18) |
| `scheduler` | object | defaults | reminders and jobs, see below |
| `heartbeat` | object | disabled | periodic proactive check, see below |
| `sandbox` | object | defaults | command sandbox, see below |
| `housekeeping` | object | defaults | retention, see below |
| `tool_options` | map[str, map] | `{}` | per-tool-group settings, e.g. `web` |
| `approval_timeout_s` | int | `300` | unanswered `/approve` requests are denied after this |
| `admins` | list[str] | `[]` | `"<channel>:<sender_id>"` allowed to use `/pair` in chat (CLI is always admin) |
| `mcp_servers` | list | `[]` | |
| `channels` | map | `{}` | |
| `identities` | map[str, list[str]] | `{}` | person to `["channel:sender_id", ...]` |

### `models`

| Key | Type | Default | Notes |
|---|---|---|---|
| `default` | str | required | profile name |
| `fallback` | list[str] | `[]` | ordered failover profiles used when resolving the default |
| `aux` | str? | none | profile for background chores (memory extraction, summaries) |
| `profiles.<name>.provider` | str | required | registry key (see spec 03) |
| `profiles.<name>.model_id` | str | required | provider-specific model identifier |
| `profiles.<name>.params` | map | `{}` | generation parameters |
| `profiles.<name>.client` | map | `{}` | connection settings (keys, region, base URL, host) |

### `memory`

| Key | Type | Default | Notes |
|---|---|---|---|
| `backend` | str | `sqlite` | memory-backend registry key: `sqlite` (keyword) or `hybrid` (keyword + semantic; needs `options.embedder`) |
| `scope` | str | `user` | `shared` = one namespace for everyone; any other value = per user |
| `options` | map | `{}` | passed to the backend constructor |
| `inject_max_entries` | int | `5` | facts injected per model call |
| `auto_extract` | bool | `true` | distil facts from conversations |
| `extract_every_n_turns` | int | `5` | extraction cadence (positive integer) |
| `allow_agent_writes` | bool | `true` | expose `add_memory` to persistent agents |

### `conversation`

| Key | Type | Default | Notes |
|---|---|---|---|
| `strategy` | str | `summarizing` | `sliding_window` selects the sliding window; any other value selects summarizing |
| `window_size` | int | `40` | sliding-window size |
| `preserve_recent_messages` | int | `10` | summarizing: recent messages kept verbatim |
| `summary_ratio` | float | `0.3` | summarizing: fraction of history summarised when trimming |

### `tasks`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | disables workers; `start_task` then queues without execution |
| `workers` | int | `2` | concurrent task workers |
| `max_attempts` | int | `3` | total attempts including the first |
| `task_timeout_s` | int | `3600` | per-attempt timeout |
| `agent` | str? | none | fallback agent when a task has none recorded |

### `storage`

| Key | Type | Default | Notes |
|---|---|---|---|
| `backend` | str | `sqlite` | `sqlite` (one local file), `memory` (not persisted), or any registered backend |
| `options` | map | `{}` | passed to the backend builder via the config |

### `logging`

| Key | Type | Default | Notes |
|---|---|---|---|
| `level` | str | `INFO` | base level for `kiro_crew.*`; validated |
| `areas` | map | `{}` | per-area overrides (`gateway`, `agents`, `tasks`, `scheduler`, `memory`, `tools`, `channels`, `security`, `web_ui`, `strands`, or a raw logger name) |
| `file` | str? | none | also write a rotating log file |
| `json` | bool | `false` | JSON lines instead of text |
| `console`, `quiet_console` | bool | `true`, `false` | `quiet_console` keeps the terminal at INFO while file/dashboard capture DEBUG |
| `ring_capacity` | int | `2000` | events kept in memory for the dashboard |
| `strands_level` | str? | none | Strands' own logger level (capped at WARNING while content hiding is on) |
| `include_content` | bool | `false` | log message text, tool arguments/results and commands (**hidden by default**) |
| `redact_secrets` | bool | `true` | mask token shapes and this config's exact secrets in every log line |

### `web_ui`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | |
| `host`, `port` | str, int | `127.0.0.1`, `8787` | |
| `token` | str? | none | required to view; enabling without one emits a warning |

### `scheduler`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | |
| `timezone` | str | `UTC` | IANA name; validated at load. Used for cron and naive `at` times |
| `tick_s` | int | `15` | polling interval |
| `max_jobs_per_user` | int | `50` | active schedules per owner |

### `heartbeat`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | |
| `every` | str | `2h` | duration |
| `target` | str? | none | `"<channel>:<conversation_id>"` to notify (required when enabled) |
| `agent` | str? | entry agent | |
| `user` | str | `shared` | memory owner for heartbeat turns |
| `ok_token` | str | `HEARTBEAT_OK` | a reply starting with this is swallowed |

### `sandbox`

| Key | Type | Default | Notes |
|---|---|---|---|
| `backend` | str | `docker` | `docker`, `host` (**unsafe**) or `none` |
| `image` | str | `python:3.12-slim` | |
| `network` | bool | `false` | allow outbound network inside the sandbox |
| `timeout_s` | int | `60` | |
| `memory`, `cpus` | str | `512m`, `1` | container limits |
| `max_output_chars` | int | `20000` | |

### `housekeeping`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | |
| `interval_h` | int | `24` | |
| `session_retention_days` | int | `90` | idle conversations older than this are deleted (`0` = keep) |
| `task_retention_days` | int | `30` | finished tasks older than this are deleted (`0` = keep) |

### `tool_options.web`

`search` (`brave`/`tavily`/`searxng`), `api_key`, `url`, `allow_private` (default `false`). See spec 16.

### `agents.<name>`

| Key | Type | Default | Notes |
|---|---|---|---|
| `description` | str | `""` | shown to a parent agent as the delegation tool description |
| `model` | str? | default | profile name |
| `system_prompt` | str | `You are a helpful assistant.` | role instructions |
| `tools` | list[str] | `[]` | tool specs, see spec 09 |
| `mcp_servers` | list[str] | `[]` | names from top-level `mcp_servers` |
| `delegates` | list[str] | `[]` | agents exposed as `ask_<name>` tools |
| `system_prompt_file` | str? | none | read the prompt from a file (relative to the config file) instead of `system_prompt` |
| `isolated` | bool | `false` | low-trust agent: no `SOUL.md`/`MEMORY.md`, private files folder, private long-term memory (spec 08) |
| `soul`, `core_memory` | bool? | yes unless `isolated` | inject `SOUL.md` / `MEMORY.md`; `core_memory: false` forbids the `core_memory` tool |
| `files`, `memory` | `shared`\|`private`? | shared unless `isolated` | per-agent files folder / long-term memory namespace |
| `stateful` | bool | `false` | as a delegate, keep its own persisted conversation across calls |
| `selectable` | bool | `false` | users may switch to it with `/agent` |
| `require_approval` | list[str] | `[]` | tool names that need the user's `/approve` in chat before running |

### `mcp_servers[]`

| Key | Type | Notes |
|---|---|---|
| `name` | str | also used as the tool-name prefix |
| `command`, `args`, `env` | str, list, map | stdio transport |
| `url`, `headers` | str, map | streamable-HTTP transport (wins if `url` is set) |

### `channels.<name>`

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | only enabled channels are constructed |
| `allow_from` | list[str] | `[]` | permitted sender ids; `"*"` allows all; empty allows none |
| `agents_allowed` | list[str]? | none | narrow which `selectable` agents users may switch to on this channel |
| `access` | str | `allowlist` | `allowlist` ignores unknown senders; `pairing` gives them a one-time code an admin approves (spec 15). Other values are rejected |
| `agent` | str? | entry agent | route this channel to a specific agent |
| *adapter keys* | any | | see spec 04 |

The channel `<name>` MUST match a registered channel (`cli`, `discord`, `slack`, `telegram`,
`whatsapp`, or a plugin).

## Secrets guidance

- **CFG-7** Secrets SHOULD be supplied through `${ENV}` references, never literal values.
- **CFG-8** `kiro-crew.yaml` is listed in `.gitignore` by default.

## Acceptance criteria

- Unknown top-level key is rejected *(follows from `extra="forbid"`; not separately exercised)*.
- Referencing a non-existent profile, agent or MCP server aborts startup *(implemented; not separately exercised)*.
- `${VAR:-x}` expansion yields `x` when unset *(implemented; the sample config uses it and loads)* *(verified)*.
- The shipped `default_config.yaml` loads and `kiro-crew check` succeeds *(verified)*.
