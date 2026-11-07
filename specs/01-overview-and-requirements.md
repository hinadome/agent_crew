# 01. Overview and requirements

## Purpose

kiro-crew is a self-hosted assistant that a person (or a small team) talks to through chat platforms.
It uses one or more LLMs, remembers what matters across conversations, delegates to specialist agents,
and executes long jobs in the background, reporting back when done.

## Goals

1. Run the same agent crew behind many chat channels.
2. Make the LLM a configuration choice, including failover and per-conversation override.
3. Provide memory that scales from a single chat to months of use without unbounded prompt growth.
4. Support work that outlives a single request and survives restarts.
5. Keep every integration point replaceable without editing core code.

## Non-goals

- Multi-tenant SaaS hosting, billing, or user management UI.
- A visual workflow designer.
- Voice, image or file transfer through channels (text only today).
- Horizontal scaling across nodes.

## Actors

| Actor | Description |
|---|---|
| End user | Sends messages from a channel; must be on that channel's allow-list (CLI excepted) |
| Operator | Edits YAML, workspace files, runs the process, reads logs |
| Plugin author | Adds providers, channels, tools or memory backends |
| Agent | An LLM-driven worker defined under `agents:` |
| Task worker | Background loop executing queued tasks with a dedicated agent |

## Glossary

| Term | Meaning |
|---|---|
| Address | `(channel, conversation_id, thread_id?)`: where a conversation lives |
| Session | Persisted message history for one agent in one conversation, keyed by `Address.key` |
| Profile | A named model configuration (`provider`, `model_id`, `params`, `client`) |
| Entry agent | The agent that receives chat messages unless a channel overrides it |
| Namespace | Partition key for long-term memory, `user:<owner>` |
| Core memory | `SOUL.md` and `MEMORY.md` injected into every system prompt |
| Task | A queued unit of background work with its own session |
| Origin | The address a task was started from; results are delivered there |

## Functional requirement summary

| Id | Requirement | Spec |
|---|---|---|
| FR-1 | Receive text from CLI, Discord, Slack, Telegram, WhatsApp and reply in the same place | 04, 05 |
| FR-2 | Reject messages from senders not on the allow-list | 05, 11 |
| FR-3 | Select LLM by profile; allow failover chain and per-conversation override | 03, 05 |
| FR-4 | Persist conversation history and restore it after restart | 06 |
| FR-5 | Store and recall durable facts per user; inject relevant facts automatically | 06 |
| FR-6 | Keep persona and standing notes in editable files | 06 |
| FR-7 | Define multiple agents; let one call another as a tool | 08 |
| FR-8 | Run long jobs in the background with progress, cancel, retry and restart recovery | 07 |
| FR-9 | Expose tools from a registry, importable modules, and MCP servers | 09 |
| FR-10 | Add providers, channels, tools, memory backends from external code | 10 |
| FR-11 | Validate configuration at startup with actionable errors | 02 |

## Non-functional requirements

| Id | Requirement |
|---|---|
| NFR-1 | **Durability:** an acknowledged message and every persisted turn survive process restart |
| NFR-2 | **Isolation:** one conversation's slow turn MUST NOT block other conversations |
| NFR-3 | **Ordering:** messages in one conversation are processed in arrival order, one at a time |
| NFR-4 | **Portability:** Python 3.11+, no required services besides the chosen LLM |
| NFR-5 | **Optional dependencies:** channel libraries are required only for enabled channels |
| NFR-6 | **Observability:** key events are logged with logger names under `kiro_crew.*` |
| NFR-7 | **Safety:** default configuration MUST NOT allow anonymous access from chat platforms |
| NFR-8 | **Bounded prompt size:** core memory capped at 6000 chars; long-term injection capped by `inject_max_entries` |

## Constraints and assumptions

- A single process owns the database. Multiple processes on one database are unsupported.
- The operator supplies LLM credentials through the environment or the provider's standard mechanism.
- Strands Agents 1.64+ supplies the agent loop, session manager, memory manager and model router.

## Acceptance criteria

- A fresh checkout can run `kiro-crew init && kiro-crew check` and report "config OK" *(verified)*.
- `kiro-crew chat` answers `/help`, `/model`, `/tasks` without an LLM call *(verified)*.
- The end-to-end scenario in spec 05 passes against a scripted model *(verified)*.
