# 12. CLI and operations

Source: `cli.py`, `app.py`.

## Commands

```
kiro-crew [-c CONFIG] [-v] <command>
```

| Command | Behaviour |
|---|---|
| `init` | copies the packaged `default_config.yaml` to `CONFIG` (default `./kiro-crew.yaml`); refuses to overwrite |
| `check` | validates config, builds the app without starting channels; prints registered providers, tools, memory backends, channels, and enabled channels |
| `chat` | same as `run --only cli` |
| `run [--only NAME…]` | starts enabled channels (optionally only the named ones), task workers, scheduler and housekeeping; runs until signalled |
| `pairing list` / `approve <code>` / `revoke <channel> <sender>` | offline access management (works while the bot runs; takes effect immediately) |
| `memory [--user OWNER] list` / `search <q>` / `forget <id>` / `clear --yes` / `reindex` | inspect and manage long-term memory (`reindex` needs the `hybrid` backend) |
| `schedule list` / `cancel <id>` | inspect or cancel reminders and scheduled jobs (including the system heartbeat) |
| `prune` | run housekeeping once and print how many sessions/tasks were removed |
| `agent list` / `show NAME` / `add NAME ...` / `validate` / `remove NAME` / `reload` | manage agents (spec 20); `reload` sends SIGHUP to the running `kiro-crew run` found via `<workspace>/kiro-crew.pid` |

- **OPS-1** Logging flags: `-v`/`--log-level`, repeatable `--log AREA=LEVEL`, `--log-file`, `--log-json`, `--log-content` (include message text/tool args, hidden by default). They
  override the `logging:` config block and apply to every command. Strands' logger is quiet (WARNING) unless the base
  level is DEBUG or `strands_level` is set. See spec 18.
- **OPS-2** Exit code 0 on clean shutdown; configuration errors abort with a non-zero exit and message.
- **OPS-3** `run` with no enabled channels exits with an explanatory message.

## Workspace layout

```
<workspace>/
  SOUL.md  MEMORY.md  HEARTBEAT.md (created when the heartbeat is enabled)
  files/                  agent sandbox
  kiro-crew.db (+ -wal, -shm)
```

Created on first start. Back up by copying the directory while the process is stopped (or use SQLite's
online backup for the database).

## Logging

Python `logging` to stderr, format `time LEVEL logger: message`. Notable events:

| Logger | Events |
|---|---|
| `kiro_crew.gateway` | channel started; unauthorised sender (WARNING); turn errors (ERROR + traceback) |
| `kiro_crew.tasks.runner` | task attempt start; failures with traceback; re-queued count on startup |
| `kiro_crew.app` | shutdown; logging configured |
| `kiro_crew.agents.trace` | per-turn tool/model/message DEBUG trace |
| `kiro_crew.web` | dashboard start, request errors |
| `kiro_crew.core.registry` | plugin load, override warnings, plugin import failures |

## Process lifecycle

1. Load/validate config → import `plugins` → create workspace and database (older schemas are upgraded in place) → build providers/tools lazily.
2. Construct enabled channels (missing credentials/dependencies raise here; no event loop exists yet).
3. Start channels, recover interrupted tasks, start task workers, the scheduler (registers the heartbeat), housekeeping and the agent-file watcher.
3b. Install the SIGHUP (reload agents) handler, **then** write `<workspace>/kiro-crew.pid` (SIGHUP terminates a process that does not handle it).
4. Wait for SIGINT/SIGTERM or a channel `quit`.
5. Stop the watcher, housekeeping, scheduler and workers (tasks remain `running`), stop channels, close database.

## Deployment notes

- **Single instance.** One process per database.
- **Supervision.** Run under systemd, a container with restart policy, or similar. Restarts are safe:
  conversations and queued/running tasks resume.
- **Networking.** Discord, Slack (Socket Mode), Telegram (polling) need only outbound HTTPS.
  WhatsApp needs inbound HTTPS to `host:port/path`; terminate TLS at a proxy or tunnel.
- **Credentials.** Provide through environment (systemd `EnvironmentFile`, container secrets). For
  Bedrock use the instance/task role or `aws login`.
- **Resource use.** Memory grows with the agent cache (≤ 64 agents) and conversation lengths; SQLite
  file grows with history (no automatic pruning today).

## Acceptance criteria

- `init` then `check` succeeds in an empty directory *(verified)*.
- `chat` handles `/help`, `/model`, `/tasks` and exits cleanly on EOF *(verified)*.
- Clean shutdown on SIGINT *(by implementation; EOF path verified)*.
- Admin subcommands (`pairing`, `memory`, `schedule`, `prune`) operate on a config/DB without starting channels *(verified)*.
- Running under a process supervisor, container image, and systemd unit *(not provided)*.
