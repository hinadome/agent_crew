# 08. Agents and crew

Source: `agents/factory.py`, `core/config.py` (`AgentConfig`), `memory/factory.py`, `memory/workspace.py`,
`core/context.py`, `agents/trace.py`.

`AgentFactory.create()` is the single place where Strands agents are assembled. Agent *management* (drop-in files,
routing, reload, CLI) is specified in [spec 20](20-agent-management.md); this spec covers what an agent is, what it may see,
and how agents work together.

## `create()` inputs

| Parameter | Purpose |
|---|---|
| `agent_name` | key under `agents:`; default `entry_agent` |
| `session_id` | persistent session id, or `None` for an ephemeral agent (stateless delegates) |
| `user_id` | memory owner (spec 05 GW-9) |
| `origin` | `Address` for task notifications and approvals |
| `model_profile` | per-conversation override |
| `extra_prompt` | appended to the system prompt (task instructions) |
| `task_id` | marks a task agent (adds `report_progress`, removes `tasks`/`schedule`) |

## Agent settings

| Key | Default | Meaning |
|---|---|---|
| `description` | `""` | shown to a parent as the delegation tool description; shown in `/agent` |
| `model` | default profile | profile name |
| `system_prompt` / `system_prompt_file` | generic | role instructions inline or from a file (one of the two) |
| `tools`, `mcp_servers`, `require_approval` | `[]` | tool groups, MCP servers, tools that need `/approve` |
| `delegates` | `[]` | other agents exposed as `ask_<name>` tools |
| `isolated` | `false` | **one switch for a low-trust agent** (see Isolation) |
| `soul` / `core_memory` | yes unless isolated | inject `SOUL.md` / `MEMORY.md` into the prompt |
| `files` | `shared` unless isolated | `shared` = `files/`, `private` = `files/agents/<name>/` |
| `memory` | `shared` unless isolated | long-term memory namespace: `shared` or `private` per agent |
| `stateful` | `false` | as a delegate, keep its own conversation across calls |
| `selectable` | `false` | users may switch to it with `/agent` |

Names: `^[A-Za-z][A-Za-z0-9_-]{0,31}$`.

- **AGT-N1** Names are validated at load time with a message explaining why: they become tool names (`ask_<name>`),
  and LLM provider APIs restrict tool names to letters, digits, `_` and `-`. Before this rule, `research bot` was accepted
  and produced the unusable tool `ask_research bot`.

## Assembly steps

1. **AGT-1 Tools.** `resolve_tools(agent.tools, ctx.for_agent(name))` (spec 09). The *per-agent* context is what lets the
   `files` and `sandbox` tools honour isolation. Then MCP clients, delegate tools (if depth < 2), `report_progress` for tasks.
2. **AGT-2 Prompt.** `system_prompt` + owner context + `extra_prompt`, where the owner context is built from the agent's own
   `soul` / `core_memory` settings (empty for an isolated agent).
3. **AGT-3 Model.** `models.resolve(model_profile or agent.model)`; aux model from `models.aux` if set.
4. **AGT-4 Conversation manager.** Per spec 06 layer 1.
5. **AGT-5 Memory.** `MemoryManager` over the store for the agent's namespace (below); extraction and `add_memory` only for
   persistent top-level agents.
6. **AGT-6 Session.** `RepositorySessionManager` over the storage backend's session store when `session_id` is set. Agents are
   keyed `(session id, agent name)`, so **different agents in one conversation keep separate histories**, and switching back
   restores each agent's own.
7. **AGT-7 State.** After construction, agent state `user_id`, `agent_name`, `origin` is set (tools read it via `ToolContext`).
8. **AGT-8 Hooks.** `TraceHook` (debug tracing) and, when `require_approval` is set, `ApprovalHook`.

## Isolation (what an agent may see of the owner's private state)

The first review of multi-agent support found that **every agent received the owner's `SOUL.md` and `MEMORY.md`**, shared one
`files/` folder and one memory namespace. A public-facing agent could therefore leak private notes. Now:

- **AGT-I1** `isolated: true` gives the agent **no** `SOUL.md` / `MEMORY.md` in its prompt, its own `files/agents/<name>/`
  sandbox (used by `files`, `sandbox` tools), and its own long-term memory namespace
  `agent:<name>:user:<owner>` (also under `memory.scope: shared`). Its persona comes only from its own prompt.
- **AGT-I2** Each aspect can be set independently (`soul`, `core_memory`, `files`, `memory`); explicit settings win over the
  `isolated` default.
- **AGT-I3** An agent that cannot see core memory MUST NOT be able to write it: configuring the `core_memory` tool on such an
  agent is a **config error**.
- **AGT-I4** Isolation is evaluated on the *callee*: a trusted parent delegating to an isolated agent does not leak its
  context into it, and a background task or scheduled job started by an isolated agent runs isolated.
- **AGT-I5** `/memory` and `kiro-crew memory --agent NAME` operate on the namespace of the agent actually in use.
- **AGT-I6** Direction of trust: a shared (trusted) agent can read `files/agents/<name>/` (it is inside `files/`); an isolated
  agent cannot read outside its own folder. Isolation protects the owner from the agent, not the agent from the owner.
- **AGT-I7** What isolation does **not** cover: tools you grant the agent (web, shell, MCP servers) run with the process's
  authority; network egress is shared; the LLM provider sees whatever you put in the prompt. Give a public agent only the
  tools it needs, and require approval for risky ones.

## Crew composition

- **AGT-8a** `delegates: [x, y]` adds an async tool per delegate named `ask_<name>`, described by the delegate's `description`.
- **AGT-9** Delegate (sub) agents get memory read access (injection + `search_memory`) but never write, even when stateful.
- **AGT-10** Delegation depth is capped at 2 (`MAX_DELEGATION_DEPTH`): beyond that no delegate tools are added.
- **AGT-11** Delegates use their own model, tools and prompt, so a crew can mix providers.
- **AGT-12** A delegate's tool calls and reasoning are not persisted in the parent's history; only the returned text is.
- **AGT-13** Config validation rejects self-delegation and warns (once per cycle) about cycles such as `a -> b -> a`; they are
  contained by the depth cap but are rarely intended.

### Stateless vs stateful delegates

| | stateless (default) | `stateful: true` |
|---|---|---|
| Each call | a fresh sessionless agent | the same agent, with its own conversation |
| Persistence | none | session `<parent session>~<delegate>` in the storage backend |
| Follow-ups ("and what about X?") | forgotten | remembered, also after the parent is rebuilt or the process restarts |
| Concurrent calls | run in parallel | serialised (an agent cannot be invoked concurrently) |
| Without a parent session | n/a | falls back to stateless |

- **AGT-S1** `/new` removes the conversation's own session **and** its delegate sessions (`delete_prefix`, literal match).
- **AGT-S2** Different conversations never share a stateful delegate's history.
- **AGT-S3** Stateful delegate sessions are subject to normal retention (spec 06).

### When to use what

| Need | Use |
|---|---|
| Quick specialist answer inside a turn | stateless delegate (`ask_*`) |
| Specialist that should remember the thread | `stateful` delegate |
| Work that takes minutes or must survive restart | `start_task` |
| Different persona/model/trust per channel, group or user | `routes` / `channels.<name>.agent` (spec 20) |
| Let users pick an agent | `selectable` + `/agent` (spec 20) |

## MCP servers

- **AGT-13a** `mcp_servers` define reusable connections; an agent opts in by name. `url` selects streamable-HTTP;
  otherwise `command/args/env` selects stdio. The server `name` is the tool prefix.
- **AGT-M1** **Pooled.** The factory keeps one `MCPClient` per `(server name, definition)` shared by every agent that uses
  it, pinned with a permanent consumer so Strands' reference counting never stops it between short-lived agents.
  *Measured before pooling: 5 delegate calls spawned 5 server processes; now 6 calls across 2 agent instances use 1.*
- **AGT-M2** After a config reload, pooled servers whose definition changed or was removed are stopped
  (`refresh_mcp`); `AgentFactory.close()` stops all at shutdown.

## Prompts shipped in the sample config

`main` (lead), `researcher`, `coder`. They are examples; replace freely or add your own as drop-in files.

## Acceptance criteria *(verified by the automated suite unless noted)*

- Invalid agent names are rejected with the reason; valid names always yield provider-safe tool names.
- An isolated agent's prompt contains neither the owner's persona nor core memory (a canary through the real gateway:
  default channel gets it, the isolated channel never does); explicit flags work independently; isolated agents cannot be given
  `core_memory`.
- Isolated agents have private files (cannot read the owner's files or escape their folder, including via `run_shell`) and
  private long-term memory (invisible both ways, also under `scope: shared`); `/memory` follows the routed agent.
- Delegation: stateless forgets, stateful remembers (non-vacuous: breaking it fails 4 tests), persists across a rebuilt
  parent, is scoped per conversation, is serialised, falls back without a session, and is wiped by `/new`; depth is capped.
- MCP: one pooled server for many agents/calls, kept alive between calls, stopped on `close()` and on config change
  *(real server process, integration test)*.
- Real MCP stdio server discovery and calls through the factory *(integration)*.
