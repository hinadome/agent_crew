# 20. Agent management: drop-in files, routing, switching and reload

Source: `core/agentfiles.py`, `core/config.py` (`_merge_agents`, `RouteRule`), `agents/manage.py`, `agents/reload.py`,
`routing.py`, `gateway.py`, `cli.py`.

Goal: adding or changing an agent should take seconds and no restart, safely, and the choice of *which* agent answers should be
flexible per channel, group, user and conversation.

## 1. Drop-in agent files

An agent can be a markdown file `<workspace>/agents/<name>.md`: settings in a YAML front-matter block, the system prompt as
the body.

```markdown
---
description: Answers product FAQs
model: claude
tools: [web]
isolated: true         # low-trust: no owner persona/core memory, private files + memory
selectable: true       # users may switch to it with /agent faq
delegate_of: [main]    # main gets an ask_faq tool, without editing main's definition
---
You answer product FAQs politely and briefly.
```

- **AGF-1** The file stem is the agent name and must match the name rule (spec 08). Files starting with `_` or `.`, and
  files that are not `.md`, are ignored, so `_draft.md` parks an agent.
- **AGF-2** Front-matter keys are `AgentConfig` fields, plus `delegate_of` and `system_prompt_file`. `${ENV}` is expanded in
  the front matter **only** (never in the prompt body). A body together with a `system_prompt` setting is an error.
- **AGF-3** `delegate_of: [a, b]` (or a single string) appends the agent to those parents' `delegates`, whether the parent is
  in the YAML or another file. Unknown parents are an error naming the file. The parent's definition is not rewritten on disk.
- **AGF-4** Every error names the file: unclosed or invalid front matter, unknown setting, bad name, missing prompt file,
  name defined both in the YAML and a file.
- **AGF-5** `system_prompt_file: path` (YAML agents: relative to the config file; drop-in agents: relative to the agents
  folder) reads a prompt from a file.
- **AGF-6** `agent_files: {dir, watch, interval_s}` configures the folder (default `agents`, relative to the workspace),
  auto-reload (default on) and poll interval (default 3 s).
- **AGF-7** YAML-defined and file-defined agents are equal once loaded; `Config.agent_sources` records which is which.

## 2. Validation (applies to all agents)

At load time, with clear messages instead of runtime failures: agent names; `entry_agent`; every `delegates`, `mcp_servers`,
`model` reference; **`channels.<n>.agent`, `routes[i].agent`, `tasks.agent`, `heartbeat.agent` and
`channels.<n>.agents_allowed`** (previously unchecked: a typo failed only on the first message); routes with no match field;
self-delegation (error) and delegation cycles (warning).

## 3. Selecting the agent for a message (`routing.py`)

Precedence, highest first:

1. the conversation's **override** set with `/agent <name>` (persisted in storage settings);
2. the first matching **`routes`** rule;
3. the channel's **`agent:`**;
4. **`entry_agent`**.

```yaml
routes:
  - {agent: support,  channel: discord, group: true}    # every Discord server/group chat
  - {agent: personal, channel: telegram, user: "12345"}  # one person
  - {agent: research, conversation: "C0123ABCD"}         # one Slack channel
```

- **RTE-1** A rule matches when **all** its specified fields match: `channel`, `conversation` (the DM/channel/chat id), `user`
  (sender id), `group` (`true` = group/server/channel chats, `false` = direct messages). At least one field is required.
- **RTE-2** `InboundMessage.is_group` is set by each adapter (Discord: guild message; Slack: not an IM; Telegram: chat type
  not private; WhatsApp and CLI: never).
- **RTE-3** A stale override (the agent was removed) is ignored: routing falls through and the chat keeps working.
- **RTE-4** The decision and its reason (`override` / `route[i]` / `channel` / `entry`) are logged at DEBUG and tagged on the
  turn, and `/agent` shows them.
- **RTE-5** Because agents are keyed per conversation *and* name, routing changes never mix histories; the gateway caches
  one agent per `(conversation, agent)`.

## 4. User switching: `/agent`

| Command | Effect |
|---|---|
| `/agent` or `/agents` | the agent in use and why, plus the agents you may switch to (name, description, `[isolated]` flags) |
| `/agent <name>` | switch this conversation (persisted; survives restarts) |
| `/agent reset` | back to default routing |

- **SWT-1** Users may only switch to agents with **`selectable: true`**, further narrowed by the channel's `agents_allowed`.
  Admins (`admins:` / CLI) may switch to any agent.
- **SWT-2** `selectable` defaults to **false**. Switching changes which tools and privileges answer the user, so it is opt-in
  per agent; an unlisted or unknown name is refused with the list of what is allowed.
- **SWT-3** `/new` clears the histories of every agent in the conversation (and stateful delegates) but keeps the selection,
  like `/model`.
- **SWT-4** Tasks and schedules created while switched run as that agent (they record the agent that created them).

## 5. Hot reload (`agents/reload.py`)

Triggers: the **file watcher** (config + `agents/*.md`, debounced), **SIGHUP**, the admin chat command **`/reload`**, and
`kiro-crew agent reload` (which signals the running process).

- **RLD-1** The new configuration is loaded and **fully validated before anything is applied**. On any error the old agents
  keep running and the error is reported; a bad edit cannot take the bot down.
- **RLD-2** A reload is refused if a running channel, `tasks.agent` or `heartbeat.agent` refers to an agent the new
  configuration removes.
- **RLD-3** Only agent-related settings are applied live: `agents`, `routes`, `mcp_servers`, `entry_agent`, `agent_files`. If
  anything else changed (models, channels, storage, ...), the result says **"Restart needed for: ..."** and does not apply it.
- **RLD-4** On success the cached agents are dropped and pooled MCP servers refreshed; conversation history is preserved
  (stored per conversation and agent name). The result lists added / changed / removed agents.
- **RLD-5** The watcher captures its baseline **when the configuration is loaded**, so an edit made before it starts polling is
  not missed (a race found by the tests), and it reports a given broken state **once**, not every poll, then recovers when fixed.
- **RLD-6** `kiro-crew run` writes `<workspace>/kiro-crew.pid` **only after** its SIGHUP handler is installed (SIGHUP's default
  action is to terminate the process) and removes it on shutdown.
- **RLD-7** `kiro-crew agent reload` refuses to signal a pid that does not look like kiro-crew (where the command line is
  readable), because a stale pid file may name a pid the OS has reused.
- **RLD-8** `/reload` is admin-only.

## 6. CLI (`kiro-crew agent ...`)

| Command | Behaviour |
|---|---|
| `list` | name, source (yaml/file), model, flags (isolated, selectable, stateful, delegate-of), and what routes to each agent |
| `show NAME` | effective settings (including what the isolation defaults resolve to) and the first lines of the prompt |
| `add NAME [--template basic\|public\|specialist] [--description --model --tools --prompt\|--prompt-file --delegate-of --isolated --selectable --stateful]` | creates `agents/NAME.md` |
| `validate` | loads the config + folder exactly as the app would; prints OK / the error and warnings (exit 1 on failure) |
| `remove NAME` | deletes a drop-in file |
| `reload` | signals a running `kiro-crew run` |

- **CLI-1** `add` **proves** the resulting configuration loads, and deletes the file if it does not; it never leaves a file that
  would break the running bot's next reload. It refuses duplicates and invalid names.
- **CLI-2** `remove` only deletes drop-in files (agents defined in YAML must be removed there) and **restores** the file if
  something still references the agent.
- **CLI-3** Template `public` = `isolated: true` + `selectable: true`.

## 7. Dashboard

`/api/state` includes `agent_details` (name, source, model, isolation flags, selectable, stateful, delegates, delegate-of, and
**what routes to it**) and `routes`; the page has an Agents card. System prompts are never exposed.

## Limits

- Channel settings (`channels.*`, including a channel's own `agent:` / `agents_allowed`) need a restart to change.
- The watcher polls modification time and size; it does not follow `system_prompt_file` targets outside the config and agents
  folder (use `/reload` or SIGHUP after editing those).
- No chat-driven agent *creation* (a deliberate choice: an LLM writing its own privileges); admins use files or the CLI.
- `is_group` in the Discord/Slack/Telegram adapters is exercised only by construction, not against the live services.

## Acceptance criteria *(verified by the automated suite)*

- Drop-in parsing (front matter, ignore rules, env expansion, errors that name the file), merging, `delegate_of`, prompt files,
  custom folder, and all new cross-reference validations.
- Routing precedence and AND-semantics; routes pick the agent through the real gateway; `/agent` lists only allowed agents,
  switches, resets, refuses non-selectable/unknown, lets admins pick any, is narrowed by `agents_allowed`, survives a restart,
  keeps per-agent histories separate, and a stale override does not break the chat.
- Reload: add / change / remove, no-op, five kinds of invalid edit rejected with the bot still answering, in-use-agent removal
  refused, non-agent changes reported but not applied, admin-only `/reload`, watcher add/change/remove, once-only error
  reporting and recovery, the pre-start-edit regression, and a real SIGHUP against a running app (pidfile lifecycle included).
- CLI: add / list / validate / remove behaviours including rollback and refusal cases.
- Fault injection: removing any of isolation, private files/memory, routes, overrides, the in-use-agent check, literal prefix
  matching, the watcher baseline or MCP pooling makes tests fail.
