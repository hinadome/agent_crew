# Architecture

kiro-crew is a thin, modular layer over Strands Agents. Strands provides the agent loop, tool calling,
model providers, session management, memory plugin and model router. kiro-crew adds the product
pieces around them: channels, a gateway, config-driven agents, durable tasks and a plugin system.

Diagrams use [Mermaid](https://mermaid.js.org) (rendered by GitHub and most Markdown viewers).

## 1. Design principles

1. **Everything swappable sits behind a registry or a protocol.** Core code never imports a concrete
   provider, channel, or memory backend by name.
2. **One choke point.** All inbound traffic passes through the `Gateway`; all agent construction passes
   through the `AgentFactory`. Cross-cutting rules (auth, sessions, model choice, memory scope) live
   there and nowhere else.
3. **State is durable and boring.** One SQLite file holds sessions, memory and tasks. Any process can
   be restarted without losing a conversation or a running job.
4. **Config over code.** Crew composition, model choice, tools and channels come from YAML.
5. **Safe by default.** Deny-by-default allow-lists, sandboxed file access, signed webhooks.
6. **Lean on Strands.** Memory injection, extraction, summarising, session restore, tool hooks and model failover are
   Strands features, not reimplemented here.
7. **Fail closed.** Sandbox tools refuse rather than run on the host; unanswered approvals are denials; unaddressable
   approvals are refusals; unknown senders get nothing unless pairing is on.
8. **Degrade, don't die.** If embeddings or search backends fail, memory falls back to keyword search and tools report
   errors as text instead of aborting the turn.
9. **Observable, safely.** Every unit of work carries a turn id through `contextvars`; structured logs flow to
   console/file/JSON and a read-only dashboard. Message content is hidden and secrets are masked by default, so turning
   visibility up does not leak conversations or credentials.
10. **Persistence is a backend.** Sessions, tasks, schedules, pairing and settings sit behind protocols chosen by config,
    with one behavioural contract suite every backend must pass.
11. **Roles are data, trust is explicit.** An agent is configuration (a YAML entry or a dropped-in file), added or changed live
    and validated before it takes effect; what an agent may see of the owner is an explicit, per-agent setting that defaults to
    the old behaviour and flips with one switch (`isolated`).

## 2. System context

```mermaid
flowchart LR
    subgraph Users
        U1[Terminal user]
        U2[Discord user]
        U3[Slack user]
        U4[Telegram user]
        U5[WhatsApp user]
        OP[Operator<br/>YAML - kiro-crew CLI]
    end

    subgraph kiro-crew process
        CH[Channels]
        GW[Gateway<br/>auth - pairing - commands]
        AF[AgentFactory]
        TR[TaskRunner]
        SC[Scheduler<br/>reminders - jobs - heartbeat]
        HK[Housekeeper]
        AP[ApprovalManager]
    end

    DB[(SQLite<br/>sessions - memory - tasks<br/>schedules - pairing)]
    WS[/Workspace files<br/>SOUL - MEMORY - HEARTBEAT - files//]
    LLM[[LLM providers<br/>Bedrock - Anthropic - OpenAI<br/>Ollama - LiteLLM]]
    EMB[[Embedding service<br/>optional]]
    MCP[[MCP servers]]
    WEB[[Web and search APIs]]
    DK[[Docker sandbox]]

    U1 & U2 & U3 & U4 & U5 <--> CH
    CH <--> GW
    GW --> AF
    GW <--> AP
    AF --> LLM
    AF --> MCP
    AF --> WEB
    AF --> DK
    AF <--> DB
    AF <--> WS
    AF -. hybrid memory .-> EMB
    TR <--> DB
    TR --> AF
    SC <--> DB
    SC -->|enqueue| TR
    SC -->|reminders| GW
    TR -->|notify| GW
    HK --> DB
    OP --> DB
```

## 3. Component view

```mermaid
flowchart TB
    subgraph Entry
        CLI[cli.py<br/>init - check - chat - run<br/>pairing - memory - schedule - prune]
        APP[app.py<br/>composition root]
    end

    subgraph Core
        CFG[core/config.py<br/>typed YAML + env]
        REG[core/registry.py<br/>Registry + plugin loading]
        TYP[core/types.py<br/>Address - Inbound - Outbound]
        CTX[core/context.py<br/>AppContext]
        LOGC[core/logging.py<br/>levels - per-turn trace - ring]
    end
    subgraph Observability
        WEBUI[web/__init__.py<br/>read-only dashboard + SSE]
    end

    subgraph Edge
        CHB[channels/__init__.py<br/>Channel ABC + chunking]
        CHI[cli - discord - slack<br/>telegram - whatsapp]
        GW[gateway.py]
    end

    subgraph Security
        PAIR[security/pairing.py<br/>PairingStore]
        APPR[security/approval.py<br/>ApprovalManager + hook]
    end

    subgraph Agents
        AF[agents/factory.py<br/>isolation - MCP pool - delegates]
        AM[agents/manage.py + reload.py<br/>drop-in files - reload - CLI]
        RT[routing.py<br/>which agent answers]
        PR[providers/<br/>registry + ModelFactory]
        TL[tools/<br/>registry - builtin - web - sandbox_tools]
        SBX[sandbox/<br/>docker - host - none]
    end

    subgraph Storage
        STOR[storage/<br/>protocols + registry<br/>sqlite - memory]
    end
    subgraph Memory
        DBW[memory/db.py<br/>Database + migrations]
        SR[memory/session_repo.py]
        LT[memory/longterm.py<br/>SQLiteMemoryStore]
        HY[memory/hybrid.py + embeddings.py<br/>semantic + keyword]
        WSF[memory/workspace.py]
        MF[memory/factory.py<br/>make_store]
        MT[memory/maintenance.py<br/>Housekeeper]
    end

    subgraph Tasks
        TS[tasks/store.py]
        TRN[tasks/runner.py]
        TT[tasks/tools.py]
    end

    subgraph Scheduler
        SS[scheduler/store.py]
        SP[scheduler/spec.py<br/>parse_when]
        SR2[scheduler/runner.py<br/>+ heartbeat]
        ST[scheduler/tools.py]
    end

    CLI --> APP
    APP --> CFG & CTX & GW & AF & TRN & SR2 & MT
    CHI -. subclass .-> CHB
    GW --> CHB & AF & PAIR & APPR & RT
    APP --> AM
    AM --> CFG
    AM --> AF
    AF --> PR & TL & SR & MF & WSF & APPR
    TL --> SBX
    MF --> LT & HY
    HY -. extends .-> LT
    TT --> TS
    ST --> SS & SP
    SR2 --> SS & TS
    TRN --> TS & AF
    SR & LT & TS & SS & PAIR -. implement .-> STOR
    STOR -- sqlite backend --> DBW
    AF & GW --> STOR
    MT --> SR & TS
    PR & TL & CHB & LT & HY & SBX -. register in .-> REG
    GW & AF & TRN & SR2 & TL -. structured logs .-> LOGC
    LOGC -- ring buffer --> WEBUI
    APP --> WEBUI & LOGC
```

Dependency direction is strictly downward: channels know nothing about agents, agents know nothing
about channels. The only upward links are late-bound through `AppContext.services` (`agent_factory`,
`gateway`, `task_runner`) to avoid import cycles.

## 4. Message flow (chat turn)

```mermaid
sequenceDiagram
    autonumber
    participant P as Platform
    participant C as Channel adapter
    participant G as Gateway
    participant F as AgentFactory
    participant A as Strands Agent
    participant M as Memory (SQLite)
    participant L as LLM

    P->>C: platform event
    C->>G: InboundMessage(address, user_id, text)
    G->>G: allow_from check (CLI is trusted)
    alt not allowed
        G-->>G: log sender id, drop
    end
    G->>G: acquire per-conversation lock
    alt text starts with a known /command
        G-->>C: reply (no LLM call)
    else normal turn
        G->>F: create(agent, session_id, user_id, model override) [if not cached]
        F->>M: restore session history
        G->>A: invoke_async(text)
        A->>M: search long-term memory (injection)
        A->>L: messages + injected memory + tools
        L-->>A: reply / tool calls (loop)
        A->>M: persist messages, extract facts every N turns
        A-->>G: result
        G->>C: chunked OutboundMessage(s)
        C->>P: send
    end
```

Turns within one conversation are serialised (a later message waits for the earlier one). Different
conversations run concurrently.

## 5. Agent assembly

`AgentFactory.create()` is the only place agents are built.

```mermaid
flowchart LR
    SRC1[agents.NAME in YAML] --> CFGA
    SRC2[agents/NAME.md drop-in file] --> CFGA[AgentConfig<br/>isolated - soul - core_memory<br/>files - memory - stateful]
    CFGA --> AF{{AgentFactory.create}}
    MP[model profile<br/>override or agent.model or default] --> AF
    WSC[Workspace.system_context<br/>SOUL.md + MEMORY.md<br/>only if the agent may see them] --> AF
    AF --> AG[Strands Agent]

    AG --- M[model: Model or ModelRouter]
    AG --- T[tools resolved for THIS agent<br/>files and sandbox use its own folder<br/>+ pooled MCP + delegates]
    AG --- CM[conversation manager<br/>summarizing or sliding window]
    AG --- MM[MemoryManager plugin<br/>namespace user:ID or agent:NAME:user:ID]
    AG --- SM[RepositorySessionManager<br/>session + agent name]
    AG --- ST[state: user_id - agent_name - origin]
```


### Crew and delegation

Declaring `delegates: [researcher]` exposes the researcher to `main` as an `ask_researcher` tool. The
sub-agent is built fresh per call with no session and no memory writes, and runs to completion before
returning text to the caller. Depth is capped at 2.

```mermaid
flowchart LR
    U((User)) --> Main[main]
    Main -->|ask_researcher| R[researcher]
    Main -->|ask_coder| Co[coder]
    Main -->|start_task| Q[(Task queue)]
    Q --> W[Task worker agent]
```

### Isolation, in one picture

```mermaid
flowchart TB
    OWN[(Owner's private state<br/>SOUL.md - MEMORY.md<br/>files - user memory)]
    MAIN[trusted agent<br/>isolated: false] -->|sees| OWN
    PUB[public agent<br/>isolated: true] -. never receives .-> OWN
    PUB --> PRIV[(its own files/agents/NAME<br/>and agent:NAME:user:ID memory)]
    MAIN -->|ask_public tool| PUB
```

Isolation is decided by the **callee**: a trusted parent delegating to an isolated agent does not leak its context into it.
Delegates can be `stateful` (own persisted conversation, `<parent session>~<delegate>`) and share pooled MCP servers.

### Which agent answers, and live reload

```mermaid
flowchart LR
    M[inbound message] --> O{conversation override<br/>/agent NAME ?}
    O -- yes --> A[agent]
    O -- no --> R{first matching route<br/>channel - conversation - user - group}
    R -- yes --> A
    R -- no --> C{channel agent?}
    C -- yes --> A
    C -- no --> E[entry_agent] --> A
```

```mermaid
sequenceDiagram
    autonumber
    participant F as agents/*.md or kiro-crew.yaml
    participant W as Watcher / SIGHUP / reload command
    participant R as Reloader
    participant App as Running app
    F->>W: file changed (debounced, baseline taken at load)
    W->>R: reload()
    R->>R: load + validate EVERYTHING first
    alt invalid or removes an agent a running channel uses
        R-->>W: FAILED (old agents keep running)
    else valid
        R->>App: swap agents - routes - mcp - entry_agent
        R->>App: drop cached agents, refresh pooled MCP servers
        R-->>W: added / changed / removed, plus "restart needed for ..." if other settings changed
    end
```

## 6. Memory architecture

```mermaid
flowchart TB
    subgraph Prompt["What the model sees each turn"]
        SYS[System prompt<br/>role + SOUL.md + MEMORY.md]
        HIST[Recent conversation<br/>older turns summarised]
        INJ[Injected long-term facts<br/>top-N for this message]
    end

    L1[1. Working memory<br/>SummarizingConversationManager] --> HIST
    L2[2. Session memory<br/>SQLiteSessionRepository<br/>every message persisted] --> L1
    L3[3. Long-term memory<br/>sqlite: FTS5 keyword<br/>hybrid: FTS5 + embeddings, RRF<br/>namespace user:ID] --> INJ
    L4[4. Core memory files<br/>SOUL.md - MEMORY.md] --> SYS

    EX[Extraction every N turns<br/>aux model distils facts] --> L3
    TOOL[add_memory / search_memory tools<br/>/memory list - forget - clear] <--> L3
    HKP[Housekeeper] -. prunes old sessions .-> L2
    CORE[remember_core tool] --> L4
```

| Layer | Scope | Lifetime | Written by | Read by |
|---|---|---|---|---|
| Working | one conversation | context window | Strands | the model |
| Session | one conversation | until `/new` | Strands session manager | restores agent after restart |
| Long-term | one user (or shared) | permanent | extraction, `add_memory` | automatic injection, `search_memory` |
| Core files | whole workspace | permanent | you, `remember_core` | every system prompt |

Memory scope is decided in `Gateway._user_key`: `shared` (one bucket) or per sender, optionally linked
across channels through `identities`.

## 7. Background task lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: start_task
    queued --> running: worker claims (atomic)
    running --> succeeded: agent finishes
    running --> queued: error and attempts left (backoff)
    running --> failed: error and attempts exhausted
    queued --> cancelled: /cancel
    running --> cancelled: /cancel
    running --> queued: process restart (recover_interrupted)
    running --> waiting: ask_user / wait_for_subtasks (worker released)
    waiting --> queued: /answer, question timeout, or all sub-tasks finished
    waiting --> cancelled: /cancel
    succeeded --> [*]
    failed --> [*]
    cancelled --> [*]
```

```mermaid
sequenceDiagram
    participant Main as Chat agent
    participant S as TaskStore
    participant W as Worker
    participant T as Task agent (session task-ID)
    participant G as Gateway
    participant U as Origin chat

    Main->>S: start_task(title, goal) records origin address
    Main-->>U: "Started task ID"
    W->>S: claim_next()
    W->>T: invoke with goal (or "continue" prompt on retry)
    loop milestones
        T->>S: report_progress(note)
    end
    T-->>W: final deliverable
    W->>S: finish(succeeded, result)
    W->>G: send(origin, result)
    G->>U: chunked message
```

Resumability comes from giving each task its own persisted Strands session. On a retry or after a
restart the agent reloads its full history and is told to continue (or, after an error, what failed).

Around this loop sit the autonomy features of [spec 21](specs/21-autonomy.md): a verifier that checks the result against `done_when`
and sends the agent back, a plan + notes kept in the task row and re-injected into the system prompt before every model call,
`ask_user` and sub-tasks that park the task as `waiting` without holding a worker, and a budget hook (tool calls, repeated calls,
tokens) that steers first and stops second.

## 8. Scheduling and heartbeat

```mermaid
sequenceDiagram
    participant Agent as Chat agent
    participant S as ScheduleStore
    participant L as Scheduler loop (every tick_s)
    participant T as TaskStore + workers
    participant G as Gateway
    participant U as Origin chat

    Agent->>S: remind_me / schedule_job (when, origin, owner)
    loop each tick
        L->>S: claim_due() - advance next_run in one transaction
        alt mode = message
            L->>G: send "Reminder: text"
            G->>U: reminder (no LLM)
        else mode = task
            L->>T: enqueue task (origin, owner)
        else mode = heartbeat
            L->>T: enqueue task with HEARTBEAT.md checklist + OK token
        end
    end
    T-->>G: result (suppressed if it starts with the OK token)
    G->>U: Task done message
```

## 9. Access control flows

```mermaid
sequenceDiagram
    autonumber
    participant X as Stranger
    participant G as Gateway
    participant P as PairingStore
    participant A as Admin (CLI or chat)

    X->>G: message
    G->>G: allow_from or approved pairing?
    alt no, access = allowlist
        G-->>G: drop + log sender id
    else no, access = pairing
        G->>P: request(channel, sender)
        P-->>G: code (reused if live), notify at most every 10 min
        G->>X: "ask the owner to approve CODE"
        A->>P: approve(CODE)  [kiro-crew pairing approve / /pair approve]
        X->>G: message
        G->>P: is_approved? yes
        G->>X: normal reply
    end
```

```mermaid
sequenceDiagram
    autonumber
    participant M as LLM
    participant H as ApprovalHook (BeforeToolCall)
    participant AM as ApprovalManager
    participant U as User chat
    participant T as run_shell (sandbox)

    M->>H: call run_shell(command)
    H->>H: tool in require_approval? has origin chat?
    H->>AM: request(origin, description)
    AM->>U: "Approval needed ... /approve or /deny"
    alt /approve (bypasses turn lock)
        U->>AM: /approve
        AM-->>H: true
        H->>T: execute in Docker sandbox
        T-->>M: output
    else /deny or timeout
        AM-->>H: false
        H-->>M: cancelled: user did not approve
    end
```

## 10. Extensibility model

```mermaid
flowchart LR
    subgraph Registries
        P[providers]
        C[channel_registry]
        T[tool_factories]
        M[memory_backends]
        E[embedders]
        SB[search_backends]
        XB[sandbox_backends]
        STB[storage_backends]
    end
    D["decorator<br/>@registry.register"] --> Registries
    E[entry points<br/>kiro_crew.*] --> Registries
    Y[plugins: in YAML<br/>module import] --> D
    Registries --> APP[App wiring by name]
```

Lookup is by string name coming from YAML, so adding an implementation never touches core code.
Entry points load lazily on first lookup; a broken third-party plugin is logged and skipped.

## 11. Data model (SQLite, WAL)

```mermaid
erDiagram
    sessions ||--o{ session_agents : has
    session_agents ||--o{ session_messages : has
    sessions ||--o{ session_multi_agents : has
    memories ||--|| memories_fts : indexed_by
    tasks }o--|| sessions : "uses task-ID session"
    memories ||--o| memory_vectors : "optional embedding"
    schedules }o--o{ tasks : "enqueue"

    sessions {
        text session_id PK
        text data
    }
    session_agents {
        text session_id PK
        text agent_id PK
        text data
    }
    session_messages {
        text session_id PK
        text agent_id PK
        int message_id PK
        text data
    }
    memories {
        int id PK
        text namespace
        text content
        real created_at
    }
    tasks {
        text id PK
        text status
        text goal
        int attempts
        text progress
        text result
        real not_before
    }
    conversation_settings {
        text session_key PK
        text model_profile
    }
    memory_vectors {
        int memory_id PK
        text model
        int dim
        blob vec
    }
    schedules {
        text id PK
        text kind
        text expr
        text mode
        real next_run
        int enabled
        text quiet_token
    }
    pairing_requests {
        text channel PK
        text sender_id PK
        text code
        real expires_at
    }
    pairing_approved {
        text channel PK
        text sender_id PK
        real approved_at
    }
```

`memories` has `UNIQUE(namespace, content)` so at-least-once extraction writes are idempotent. This schema is the `sqlite` storage backend's; the `memory` backend holds the same entities in process. `memory_vectors` rows are
deleted with their memory (`ON DELETE CASCADE`). Columns added after v0.1 (`tasks.quiet_token`,
`session_messages.updated_at`) are added by in-place migrations on start (`Database.ensure_column`).

## 12. Concurrency and failure model

| Concern | Approach |
|---|---|
| Two messages in one conversation | per-conversation `asyncio.Lock` (weakly held, auto-freed) |
| Many conversations | concurrent; at most 64 agents cached (LRU), rebuilt from session on miss |
| Blocking SQLite from async code | single connection behind an `RLock`; operations are short |
| Task claimed twice | `claim_next` runs in `BEGIN IMMEDIATE` |
| Crash mid-task | status stays `running`; next start re-queues and resumes from the session |
| LLM error in a task | retry with backoff `min(30 * 2^(n-1), 600)` s up to `max_attempts` |
| Channel send failure for a task result | logged; the task result stays in the store |
| Graceful shutdown | stop workers (tasks remain `running`), stop channels, close DB |
| Bad third-party plugin | logged and skipped at entry-point load |
| Scheduler tick crash | logged; loop continues; one schedule failing does not block others |
| Schedule dispatched but process dies before completion | lease expires and the schedule is re-claimed (at-least-once); may fire twice |
| Approval nobody answers | auto-denied after `approval_timeout_s`; the turn continues |
| Embedding service down | facts still saved; search falls back to keyword; `reindex` heals |
| Docker missing | sandbox tools return an error; never run on the host |
| Channel built before the event loop exists | adapters create loop-bound objects in `start()` (a Slack bug of this kind was caught by tests) |
| Log emitted from a worker thread | ring buffer marshals fan-out onto the loop (`call_soon_threadsafe`) |
| Slow web-dashboard viewer | its per-subscriber queue drops events rather than blocking logging |
| Field name collides with a LogRecord attribute | renamed with an `f_` prefix instead of raising |

## 13. Security model

| Threat | Mitigation |
|---|---|
| Strangers using the bot | `allow_from` deny-by-default; sender id logged for onboarding |
| Group chat noise or abuse | Discord, Telegram: reply only when mentioned; Slack: thread replies |
| Agent file access | `files` tool resolves paths inside `workspace/files`; escape raises `PermissionError` |
| Forged WhatsApp webhooks | HMAC-SHA256 of the raw body (`X-Hub-Signature-256`), constant-time compare; verify token |
| Duplicate/retried webhooks | message-id de-duplication window (2000 ids) |
| Secrets in config | `${ENV}` expansion; `kiro-crew.yaml` is git-ignored |
| Cross-user memory leaks | memory namespace per user (or explicit `shared`) |
| FTS query injection | search terms are tokenised and individually quoted |
| Runaway delegation | depth cap of 2; task agents cannot start tasks or schedules |
| Stranger enumeration / flooding in pairing mode | random one-time codes, 1 h expiry, pending queue capped at 20, replies rate-limited |
| Dangerous tool calls | `require_approval` pauses the call until the user types `/approve`; unanswered = denied; no chat = refused |
| Agent-run code harming the host | Docker sandbox: no network, no capabilities, read-only root, resource limits, only `files/` mounted; fails closed without Docker |
| SSRF through `web_fetch` | public addresses only, every redirect hop re-validated, size/time caps; content labelled untrusted |
| Unbounded growth of stored data | retention housekeeping for sessions and finished tasks |

Not covered: robust prompt-injection defence (web content is labelled untrusted and risky tools can require approval, but
neither is a guarantee), per-user rate limiting and budgets, and sandboxing of third-party tools, vended Strands
`shell`/`python_repl`, or MCP servers. (`web_fetch` now pins the connection to the vetted IP, closing the
DNS-rebinding window; still restrict the process's egress as defence in depth.) Treat enabled tools as having the agent's full
authority.

## 14. Key decisions

| Decision | Why | Trade-off |
|---|---|---|
| SQLite as the default storage backend | zero ops, durable, one file to back up | single node (but storage is now behind a registry, so a networked backend can replace it) |
| stdlib `sqlite3` + lock, not an async driver | Strands calls session repositories synchronously | brief event-loop blocking |
| Persistence behind five small protocols as a `Storage` bundle | swap the whole backend by config; one contract suite guards every backend | more indirection than direct SQL |
| Logs hide content and mask secrets by default | the dashboard/logging are safe to enable without leaking chats or keys | pattern-based scrubbing is best-effort |
| Keyword (FTS5) long-term search | no embedding model or service needed to start | weaker paraphrase recall |
| Strands `MemoryManager` | injection, extraction and tools are already solved | tied to its API |
| Agents-as-tools for the crew | simple, observable, no graph engine | sub-agents are one-shot and stateless |
| Per-task sessions | free crash-resume and isolated context | one extra session per task |
| Lazy channel imports | optional dependencies only needed when enabled | adapter errors surface at startup |
| Rebuild agent on `/model` | session restore makes it cheap and always correct | one-turn rebuild cost |
| Scheduler reuses the task queue | one execution path (retries, timeouts, resume, notifications) | scheduled work inherits task limits |
| Reminders bypass the LLM | cheap and reliable | no dynamic content in the reminder |
| Lease-then-complete scheduling | at-least-once: a crash never drops a firing | a crash after dispatch can fire twice |
| Hybrid memory = SQLite + in-process vectors | no extra service; graceful degradation | scan cost grows with facts per user |
| Approval via the same chat (hook + turn-lock bypass) | no separate UI needed | one prompt at a time per conversation |
| Docker for sandboxing, fail closed | strong, well-understood isolation | needs Docker; no silent fallback by design |
| Agent isolation decided by the callee, opt-in per agent | no surprise for existing setups; one switch for public-facing agents; correct under delegation and tasks | trusted agents can still read isolated agents' folders; isolation is not a tool sandbox |
| Drop-in markdown agents + `delegate_of` | adding a role needs no YAML surgery or restart; the parent need not be edited | a writable `agents/` folder is a control surface (protect it like the config) |
| Reload applies agents/routes/MCP only, validated first | a typo cannot break a live bot; risky settings stay restart-only | channel/model changes still need a restart |
| `selectable` defaults to false | switching changes whose tools answer the user, so it must be a deliberate grant | an extra setting to enable user choice |
| Pooled MCP clients with a permanent pin | one server process for many short-lived agents (measured 5 spawns -> 1) | servers live until shutdown or config change |
| Pairing as an opt-in per channel | easy onboarding without YAML edits | replies to strangers |
| Per-turn context via contextvars | one `turn_id` correlates logs across gateway/agent/tools/tasks without threading an arg everywhere | lost if work hops threads without copying context |
| In-memory ring buffer + SSE for the UI | zero deps, live streaming, no log DB | history bounded by capacity; single-node |
| Web dashboard read-only + token | visibility without a mutation attack surface | actions still go through chat/CLI |
