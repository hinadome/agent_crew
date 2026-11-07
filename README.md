# kiro-crew

A modular, multi-channel, multi-LLM agent crew built on [Strands Agents](https://strandsagents.com).
Talk to it from your terminal, Discord, Slack, Telegram or WhatsApp. It remembers you, delegates to
specialist agents, and runs long jobs in the background without blocking the chat.

- **Any LLM, easy to switch:** Bedrock, Anthropic, OpenAI (and any OpenAI-compatible server), Ollama,
  LiteLLM (100+ providers). Switch in YAML or with `/model` in chat. Optional automatic failover.
- **Layered memory:** conversation summarising, durable session history, searchable long-term facts
  (keyword or semantic+keyword), editable `SOUL.md` / `MEMORY.md`, and `/memory` to list or forget facts.
- **Long-running tasks:** durable queue, resumable sessions, retries, timeouts, cancel, progress. Tasks can declare
  completion criteria (`done_when`); the result is **verified** and the agent is sent back to fix gaps.
- **Guard rails for autonomy:** per-run tool-call budget and stuck-loop detection (steer first, then stop).
- **Acts on its own:** reminders, recurring jobs and cron schedules, plus an optional **heartbeat** that only
  speaks when something needs you.
- **Crew you can grow in seconds:** add an agent by dropping a markdown file (or `kiro-crew agent add`); a running bot picks
  it up with no restart, and a broken file is rejected while the old agents keep running. Agents can be **isolated**
  (a public-facing agent never sees your private notes), **routed** per channel / group / user, **switched by users** with
  `/agent`, and delegates can be **stateful**.
- **Useful tools, safely:** web fetch (SSRF-guarded) and search, and a Docker **sandbox** for shell/Python that
  fails closed, with optional chat approval (`/approve`) before each run.
- **Access control:** deny-by-default allow-lists, or **pairing** (strangers get a one-time code you approve).
- **Modular:** every extension point (providers, channels, tools, memory backends, embedders, search and sandbox
  backends) is a registry. Add one without editing the core.
- **Full visibility:** structured logs with per-turn tracing, adjustable per area, to console/file/JSON, plus an
  optional read-only **web dashboard** that streams logs live and shows tasks, schedules and pairing.
- **Pluggable storage:** sessions, tasks, schedules and pairing sit behind protocols; `sqlite` (default) and `memory`
  backends ship, with one behavioural contract suite both pass.
- **Safe to turn on logging:** message text and tool arguments are hidden by default and secrets are always masked.
- **Tested:** 390 hermetic tests (fake LLM, no network/keys/Docker), 30 opt-in integration tests against real
  infrastructure, mypy-clean source, CI workflow included.

> Status: the hermetic suite runs everything against a scripted fake LLM and fake channels. Real HTTPS/TLS fetching
> and a real MCP server have been exercised by the integration tests. **Real LLM providers, live Discord / Slack /
> Telegram / WhatsApp, Docker containers and real search/embedding APIs have not been run yet**: the tests exist and
> skip until you provide credentials or Docker (see [Development](#development)).
> See [Known limitations](#known-limitations).

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras          # or: uv sync   (core only; add extras per channel)
uv run kiro-crew init         # writes ./kiro-crew.yaml
uv run kiro-crew check        # validates config, lists registered plugins
uv run kiro-crew chat         # local terminal chat (no accounts needed)
uv run kiro-crew run          # start every channel with enabled: true
```

Admin commands (work on the database directly, even while the bot runs):

```bash
uv run kiro-crew pairing list|approve <CODE>|revoke <channel> <sender>
uv run kiro-crew memory [--user OWNER] list|search <q>|forget <id>|clear --yes|reindex
uv run kiro-crew schedule list|cancel <id>
uv run kiro-crew agent list|show|add|validate|remove|reload      # manage agents (see "Agents" below)
uv run kiro-crew prune        # delete old sessions / finished tasks now
```

The default config uses Amazon Bedrock. Authenticate first (`aws login`, or standard AWS credentials),
or point `models.default` at another profile such as `gpt`, `claude-direct`, or `local` (Ollama).

### Install extras

| Channel | Extra | Notes |
|---|---|---|
| CLI, Telegram | none | Telegram uses httpx long-polling |
| Discord | `uv sync --extra discord` | enable the *Message Content* intent |
| Slack | `uv sync --extra slack` | Socket Mode, no public URL |
| WhatsApp | `uv sync --extra whatsapp` | needs a public HTTPS URL for the webhook |

## Configuration at a glance

One YAML file (`kiro-crew.yaml`). `${VAR}` and `${VAR:-default}` are read from the environment, so keep
secrets out of the file. Full reference: [specs/02-configuration.md](specs/02-configuration.md).

```yaml
models:
  default: claude
  # fallback: [gpt]          # ordered failover
  # aux: haiku               # cheap model for memory extraction and summaries
  profiles:
    claude: {provider: bedrock, model_id: us.anthropic.claude-sonnet-4-5-20250929-v1:0}
    gpt:    {provider: openai,  model_id: gpt-4o, client: {api_key: "${OPENAI_API_KEY}"}}
    local:  {provider: ollama,  model_id: llama3.2}

agents:
  main:
    system_prompt: You lead a crew. Delegate focused work; use start_task for long jobs.
    tools: [time, tasks, files, core_memory]
    delegates: [researcher]
  researcher:
    description: Gathers and summarises information.
    tools: [time]

channels:
  cli:     {enabled: true}
  discord: {enabled: true, token: "${DISCORD_TOKEN}", allow_from: ["123456789012345678"]}
```

### Chat commands

| Command | Effect |
|---|---|
| `/help` | list commands |
| `/new` | start a fresh conversation (long-term memory is kept) |
| `/model [name]` | show or switch the LLM profile for this conversation |
| `/agent [name\|reset]`, `/agents` | show or switch the agent answering this conversation (only agents marked `selectable`; admins: any) |
| `/reload` | admins: reload agents from the config and `agents/` folder |
| `/tasks` | list your background tasks |
| `/cancel <id>` | cancel a background task |
| `/schedules`, `/unschedule <id>` | list or cancel your reminders and scheduled jobs |
| `/memory`, `/memory search <text>`, `/memory forget <id>`, `/memory clear confirm` | see and manage what the bot remembers about you |
| `/approve`, `/deny` | answer an approval request for a sensitive tool |
| `/pair list\|approve <code>\|revoke <channel> <sender>` | admins only |

### Allowing people to talk to the bot

`allow_from` is empty by default, which means **nobody** can use a chat platform. Two ways to add people:

1. **Allow-list (default).** Message the bot once; the log shows `Ignored message from unauthorised sender
   '<id>' ...`. Copy that id into `channels.<name>.allow_from` and restart.
2. **Pairing.** Set `access: pairing` on the channel. A stranger who messages the bot gets a one-time code; you run
   `kiro-crew pairing approve <CODE>` (or `/pair approve <CODE>` in chat if you are listed under `admins`). Access is
   immediate, no restart. Codes expire after an hour.

The local CLI is always trusted.

## Agents: add, isolate, route, switch

An agent is a role: a prompt, a model, a set of tools. The sample config ships `main`, `researcher` and `coder`.

**Add one in seconds.** Drop `<workspace>/agents/faq.md` (or run the CLI) — no YAML editing, no restart:

```markdown
---
description: Answers product FAQs
tools: [web]
isolated: true         # low-trust: never sees your SOUL.md / MEMORY.md; own files folder and memory
selectable: true       # users may switch to it with /agent faq
delegate_of: [main]    # main gets an ask_faq tool
---
You answer product FAQs politely and briefly.
```

```bash
kiro-crew agent add faq --template public --delegate-of main --prompt "You answer product FAQs politely."
kiro-crew agent list          # name, source, model, flags, and what routes to each agent
kiro-crew agent validate      # check everything exactly as the app would load it
```

A running bot reloads within a few seconds (file watcher), or on `kiro-crew agent reload` / SIGHUP / `/reload`. A reload is
validated first: **a typo can never take a running bot down** — the old agents keep running and you get the error. A reload
is also refused if it would remove an agent a running channel uses. Park a draft by naming it `_draft.md`.

**Keep private things private.** By default every agent sees your persona and core memory. For a public-facing agent set
`isolated: true`: it gets none of your `SOUL.md` / `MEMORY.md`, its own `files/agents/<name>/` folder and its own long-term
memory (each aspect can also be set separately). `isolated` protects *your notes*; it does not restrict the tools you give it.

**Choose who answers.** Per channel (`channels.discord.agent`), or with rules — first match wins, then the channel default,
then `entry_agent`:

```yaml
routes:
  - {agent: support,  channel: discord,  group: true}    # all Discord server/group chats
  - {agent: personal, channel: telegram, user: "12345"}   # one person
```

**Let users pick.** `/agent` lists the agents a user may switch to and `/agent support` switches the conversation (it
persists; each agent keeps its own history in the same chat; `/agent reset` returns to default). Only agents marked
`selectable: true` can be chosen (optionally narrowed per channel with `agents_allowed`); admins can pick any. Switching
changes whose tools answer the user, so it is opt-in.

**Specialists that remember.** Agents can call other agents as tools (`delegates`). Add `stateful: true` to a specialist so it
keeps its own conversation across calls (default: a fresh agent each call). MCP servers are pooled, so many agents and calls
share one server process. Details: [specs/20-agent-management.md](specs/20-agent-management.md).

### Reminders, jobs and the heartbeat

Just ask: "remind me at 9 tomorrow to call Sam", "every weekday at 8 summarise my open tasks". Schedules accept
`in 30m`, `at 2026-11-07 09:00`, `every 2h` and `cron 0 9 * * 1-5` (in `scheduler.timezone`). Enable
`heartbeat:` to have the agent periodically review `HEARTBEAT.md` and message you only when something needs
attention.

### Running commands and browsing the web

Give an agent the `sandbox` tool group to run shell/Python inside a locked-down Docker container (no network,
no capabilities, only the `files/` folder mounted). Without Docker it refuses to run; it never falls back to
your host. Add `require_approval: [run_shell, run_python]` to be asked in chat before every command. Give an
agent `web` for page fetching (and set `tool_options.web.search` to enable search).

## Visibility: logs and the web dashboard

Turn up detail with flags (these win over config):

```bash
kiro-crew -v chat                      # everything at DEBUG
kiro-crew --log gateway=debug --log tools=debug run   # just these areas
kiro-crew --log-file ~/.kiro-crew/k.log --log-json run  # rotating JSON log file
```

**Safe by default:** message text, tool arguments/results and commands are hidden (`<N chars hidden>`) and secrets
(token shapes, `key=value` credentials, the exact secrets in your config) are masked in every line from every logger, while
the flow stays visible (who, which agent/model, which tools, timings). Add `--log-content` / `logging.include_content: true`
only on a log sink you trust; secrets stay masked even then.

Every log line during one conversation is tagged with a `turn_id` plus the channel, user and agent, so you
can follow a message end to end: arrival → auth → which agent/model → each tool call and result → the reply.
Areas you can set independently: `gateway`, `agents`, `tasks`, `scheduler`, `memory`, `tools`, `channels`,
`security`, `web_ui`, `strands`.

Or configure it in `kiro-crew.yaml`:

```yaml
logging:
  level: INFO
  areas: {gateway: debug, tools: debug}
  file: ~/.kiro-crew/kiro-crew.log
  quiet_console: false      # keep the terminal at INFO while file/UI capture DEBUG
```

### Web dashboard

```yaml
web_ui:
  enabled: true
  host: 127.0.0.1
  port: 8787
  token: "${KIRO_WEB_TOKEN}"   # required to view
```

Open `http://127.0.0.1:8787/?token=...` to watch logs stream live (filter by level/text, follow mode) and
see current tasks, schedules and pairing requests. It is **read-only** and token-gated; it exposes
conversation metadata, so keep it on localhost or behind your own auth/TLS.

### Autonomy: completion checks and guard rails

When the lead agent starts a background task it can state `done_when` ("a file report.md exists with 3 sources"). The
worker is told the criteria up front. When it finishes, a separate tool-less model call (the `models.aux` profile if set)
judges the result: if it is not met, the same agent session is sent back with the reviewer's feedback, at most
`tasks.max_verify_rounds` times. The result is always delivered; if it is still unmet you get a visible `WARNING` with what
is missing, and if the reviewer is unusable the message says it could not be verified. Set `tasks.verify_all: true` to judge
tasks that gave no criteria against their goal, or `tasks.verify: false` to switch it off.

`limits:` caps tool calls per chat turn / task run and detects an agent repeating the identical call. Past a limit the call is
cancelled and the model is told why; if it ignores that, the run is stopped (chat: you get a message; task: it fails without
retrying). Details and what is *not* covered: [specs/21-autonomy.md](specs/21-autonomy.md).

## How it fits together

```
 channels (CLI, Discord, Slack, Telegram, WhatsApp)
        |  InboundMessage                 ^ OutboundMessage (chunked)
        v                                 |
     Gateway --> AgentFactory --> Strands Agent --> LLM provider (switchable / failover)
        |  \             |                |
        |   pairing      |                +--> tools (web, sandbox, schedule...), MCP, delegate agents
        |   approvals    +--> memory (session, long-term, core files)
        +--> TaskRunner <--> TaskStore (SQLite)   background work, notifies the origin chat
        +--> Scheduler  --> reminders / jobs / heartbeat  (enqueues tasks)
        +--> Housekeeper                          prunes old sessions and finished tasks
```

Details and diagrams: [ARCHITECTURE.md](ARCHITECTURE.md). Detailed behaviour of each feature:
[specs/](specs/README.md).

## Extending

Each extension point is a registry; register from your own module and list it under `plugins:`
(or publish it as a Python entry point).

```python
# my_pkg/plugin.py
from strands import tool
from kiro_crew.providers import providers
from kiro_crew.tools import tool_factories

@providers.register("my-llm")
def build(profile):                      # ModelProfile -> strands Model
    return MyModel(model_id=profile.model_id, **profile.params)

@tool_factories.register("hello")
def hello(ctx):                          # AppContext -> tool(s)
    @tool
    def say_hello(name: str) -> str:
        """Greet someone.

        Args:
            name: Who to greet.
        """
        return f"Hello {name}"
    return say_hello
```

```yaml
plugins: [my_pkg.plugin]
agents:
  main: {tools: [hello]}
```

| Extension | Register with | Entry-point group |
|---|---|---|
| LLM provider | `@providers.register` | `kiro_crew.providers` |
| Channel | `@channel_registry.register` (subclass `Channel`) | `kiro_crew.channels` |
| Tool | `@tool_factories.register` | `kiro_crew.tools` |
| Memory backend | `@memory_backends.register` | `kiro_crew.memory_backends` |
| Embedder | `@embedders.register` | `kiro_crew.embedders` |
| Search backend | `@search_backends.register` | `kiro_crew.search_backends` |
| Sandbox backend | `@sandbox_backends.register` | `kiro_crew.sandbox_backends` |
| Storage backend | `@storage_backends.register` | `kiro_crew.storage_backends` |

See [specs/10-extensibility.md](specs/10-extensibility.md) for contracts and a channel example.

## Project layout

```
src/kiro_crew/
  app.py            composition root (builds and runs everything)
  cli.py            `kiro-crew` command (init, check, chat, run, pairing, memory, schedule, prune)
  gateway.py        auth, sessions, commands, reply delivery
  core/             config, registry, shared types, AppContext, logging (levels, per-turn trace, ring buffer)
  providers/        LLM provider registry + ModelFactory (+ failover router)
  channels/         Channel base + cli, discord, slack, telegram, whatsapp
  agents/           AgentFactory (isolation, pooled MCP, stateful delegates), drop-in file management, hot reload
  routing.py        which agent answers a message (override > routes > channel > entry)
  storage/          persistence protocols + registry; `sqlite` and `memory` backends
  memory/           SQLite wrapper, sessions, long-term stores (sqlite/hybrid/inmem), embeddings, workspace, housekeeping
  tasks/            task store, background runner, agent-facing tools
  scheduler/        schedule parser, store, runner (+ heartbeat), agent-facing tools
  security/         pairing store, tool approval manager and hook
  web/              read-only dashboard (live log stream, tasks, schedules, pairing)
  sandbox/          Docker / host / none command backends
  tools/            tool registry, built-in tools, web fetch/search, sandbox tools
  default_config.yaml
tests/              390 hermetic tests (fake LLM + fake channel) + tests/integration (real infrastructure)
specs/              detailed functional specifications
ARCHITECTURE.md     structure, diagrams, design decisions
.github/workflows/  CI
```

## Runtime files

Everything lives under `workspace:` (default `~/.kiro-crew`):

| Path | Purpose |
|---|---|
| `SOUL.md` | persona and standing rules (you edit) |
| `MEMORY.md` | curated core memory, injected into every prompt (size-capped) |
| `HEARTBEAT.md` | the heartbeat checklist (created when `heartbeat.enabled`) |
| `files/` | the only folder the agent's file tools can read or write |
| `kiro-crew.db` | sessions, long-term memory, tasks (SQLite, WAL mode) |

## Known limitations

- Single process, single node. Do not run two instances against the same database (startup re-queues
  tasks marked `running`). Storage is pluggable, but only `sqlite` (and a non-persistent `memory`) ship.
- Long-term retrieval is keyword by default; the `hybrid` backend adds semantic search but scans vectors
  in-process (fine for thousands of facts per user, not millions).
- WhatsApp only allows free-form replies within 24 hours of the user's last message; late task results
  and reminders may need an approved template (not implemented).
- With failover enabled, Strands routes only the main model call: `structured_output()` bypasses the
  router, and a failure mid-stream shows partial output followed by the replacement's full answer.
- Schedules fire **at-least-once**: a firing is leased, and advanced only after it is dispatched, so a crash
  mid-fire re-runs the job rather than dropping it (a reminder may be re-sent; tasks re-run their turn).
- `web_fetch` blocks private addresses **and pins the connection to the vetted IP** (closing the DNS-rebinding
  window); redirects are re-validated and pinned too. The vended Strands `shell`/`python_repl` tools and
  `sandbox.backend: host` are still unsandboxed.
- As defence in depth you should still restrict the process's outbound network (egress firewall / no route to
  internal ranges), since `allow_private: true` and the host sandbox can reach wherever the process can.
- Agent isolation protects your private notes and files from an agent, not the tools you grant it; channel settings
  still need a restart (only agents/routes/MCP reload live); `is_group` is not yet verified against the live
  Discord/Slack/Telegram services.
- The web dashboard is read-only and single-node; its live log history is the in-memory ring buffer
  (size-capped) plus any configured log file. No metrics/tracing export yet.
- No rate limits or cost budgets yet.
- **Not yet exercised against real services:** LLM providers, Discord, Slack, Telegram, live WhatsApp, Docker
  containers, search and embedding APIs (the integration tests for them are written and skip until you supply
  credentials/Docker). A Slack start-up bug and a TLS-SNI dependency were each found by tests before release, so expect
  similar first-contact issues in what has not run.

See [specs/13-roadmap.md](specs/13-roadmap.md) for what is planned.

## Development

```bash
uv sync --all-extras          # include every channel library so no test is skipped
uv run ruff check src tests
uv run mypy                   # type-checks the whole source tree
uv run pytest -q -rs          # hermetic suite: ~17 s; no network, API keys or Docker needed
uv run kiro-crew -v chat      # verbose logging
```

### Integration tests (real infrastructure)

```bash
uv run pytest -m integration -rs        # -rs prints why each skipped test skipped
```

Always runnable with just a network: real HTTPS fetching (DNS, TLS, redirects, certificate rejection under IP pinning) and a
real MCP stdio server. Opt-in via `KIRO_IT_*` variables (never your normal credentials), each skipping with an explicit reason:
Docker sandbox (needs a Docker daemon), a real LLM (`KIRO_IT_PROVIDER`, `KIRO_IT_MODEL_ID`, `KIRO_IT_API_KEY`...), search APIs,
embeddings, and Telegram/Discord/Slack/WhatsApp bot connectivity. Details and the manual channel checklist:
[specs/17-testing-and-ci.md](specs/17-testing-and-ci.md).

CI runs lint, mypy, the hermetic suite and a config smoke test on Python 3.11-3.13, plus a separate integration job on pushes.
