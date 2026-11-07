# 06. Memory

Source: `memory/`, `agents/factory.py`, Strands `MemoryManager`.

Four layers, each with a different scope and lifetime. The design goal is a bounded prompt that still
gives the agent months of context.

## Layer 1: working memory

- **MEM-1** Managed by a Strands conversation manager chosen by `conversation.strategy`:
  `summarizing` (default; old turns are summarised, the most recent `preserve_recent_messages` kept
  verbatim) or `sliding_window` (oldest messages dropped beyond `window_size`).
- **MEM-2** Summaries use the agent's model; with `models.aux` set, Strands may use the aux model for
  background chores that support it.

## Layer 2: session memory

Source: `memory/session_repo.py`.

- **MEM-3** `SQLiteSessionRepository` implements Strands `SessionRepository` and is used through
  `RepositorySessionManager`. Every message is stored as JSON in `session_messages`.
- **MEM-4** On agent construction with an existing `session_id`, history and agent state are restored.
- **MEM-5** Tables: `sessions`, `session_agents`, `session_messages`, `session_multi_agents`.
  `update_*` preserve `created_at`. `delete_session` removes all rows for a session in one transaction.
- **MEM-6** Session ids: chat = `Address.key`; task = `task-<id>`; delegate agents have **no** session.
- Constraint: a Strands `RepositorySessionManager` does not support an agent `context_manager`; this
  project does not use one.

## Layer 3: long-term memory

Source: `memory/longterm.py`.

- **MEM-7** `SQLiteMemoryStore` implements the Strands `MemoryStore` protocol (`search`, `add`).
  Facts live in `memories(namespace, content, created_at)` with `UNIQUE(namespace, content)`.
- **MEM-8** Search uses SQLite FTS5 (`porter unicode61` tokenizer), ranked by BM25, scoped to the
  store's namespace. The query is built from the input's words (length > 2, lower-cased, up to 24,
  deduplicated), each double-quoted and joined with `OR`, so user text cannot inject FTS operators.
  An input with no usable words returns no results.
- **MEM-9** `add` trims content, ignores empty strings, and is idempotent (`INSERT OR IGNORE`), making
  Strands' at-least-once extraction safe. It returns whether a new row was written.
- **MEM-10** Namespace is `user:<owner>` where owner comes from `Gateway._user_key` (spec 05).
  Different owners never see each other's facts.
- **MEM-11** **Injection.** Strands' `MemoryManager` searches the store using the latest user message
  before each model call and folds the top `inject_max_entries` results into that call's input as a
  `<memory>` block. The injected text is not written to durable history.
- **MEM-12** **Extraction.** When `auto_extract` is true and the agent is persistent and top-level
  (not a delegate), the store is configured with an `IntervalTrigger(extract_every_n_turns)`.
  Strands then distils discrete facts from recent messages (tool traffic excluded) and writes them with
  `add`. A model call is made for extraction; set `models.aux` to make it cheap.
- **MEM-13** **Tools.** `search_memory` is available to every agent. `add_memory` is available only to
  persistent top-level agents and only when `allow_agent_writes` is true.
- **MEM-14** Delegate (sub) agents get read access (injection + search) but never write.

### Administration

- **MEM-14b** `SQLiteMemoryStore` also offers `list(limit, offset)` (newest first), `count()`, `delete(id)` and `clear()`,
  all scoped to the store's namespace (a user can never delete another namespace's facts). These back the chat command
  `/memory` and the CLI `kiro-crew memory list|search|forget|clear --yes|reindex [--user OWNER]`.
- **MEM-14c** `/memory clear` requires the word `confirm`; the CLI requires `--yes`.
- **MEM-14d** The chat commands act on the *sender's own* namespace only; the CLI can address any owner with `--user`.

### Hybrid (semantic + keyword) backend

`memory.backend: hybrid` with `memory.options.embedder: {provider, model, ...}`.

- **MEM-14e** Facts are embedded on write (unit-normalised float32 vectors in `memory_vectors`, with the model name).
  Search runs BM25 and cosine similarity (NumPy if installed, else pure Python) and merges the two rankings by
  reciprocal-rank fusion (k = 60), so exact names/IDs and paraphrases both work. Vector candidates must reach
  `min_similarity` (default 0.3).
- **MEM-14f** Embedders (registry `embedders`): `openai` (any OpenAI-compatible `/embeddings`, via `base_url`),
  `ollama` (`/api/embed`), `bedrock` (Titan, via boto3).
- **MEM-14g** **Graceful degradation.** If embedding fails on write, the fact is still saved and keyword-searchable; if it
  fails on search, keyword results are returned. `reindex` embeds every fact missing a vector for the current model.
- **MEM-14h** Vectors carry their model name; changing `model` makes old vectors ignored until `reindex`.
- **MEM-14i** Deleting a fact cascades to its vector. Scale: vectors are scanned per query, suited to thousands of facts per
  user; use a real vector database backend beyond that.

### Retention (housekeeping)

- **MEM-14j** Every `housekeeping.interval_h` hours (and via `kiro-crew prune`): conversations whose newest message is
  older than `session_retention_days` are deleted; finished tasks older than `task_retention_days` are deleted.
  Sessions belonging to queued/running tasks are never pruned. Long-term facts and core memory are never auto-deleted.
- **MEM-14k** Older databases are upgraded in place on start (`session_messages.updated_at`, `tasks.quiet_token`); existing
  rows get the migration time so nothing is pruned immediately.

### Backends

- **MEM-15** A backend is a callable `(db, namespace, **options) -> MemoryStore` registered in
  `memory_backends`. `memory.backend` selects it and `memory.options` plus `extraction` are passed in.
  To get semantic retrieval, register a vector/KB-backed store implementing the same protocol.

## Layer 4: core memory (workspace files)

Source: `memory/workspace.py`.

- **MEM-16** On first run the workspace creates `SOUL.md` (default persona) and `MEMORY.md`
  (`# Core memory`).
- **MEM-17** Every agent's system prompt is: `agent.system_prompt` + `<soul>…</soul><core_memory>…</core_memory>`
  + optional task prompt. `SOUL.md` is truncated to 6000 chars; if `MEMORY.md` exceeds 6000 chars only the
  newest 6000 are shown, prefixed by a truncation marker.
- **MEM-18** The `core_memory` tool (`remember_core(note)`) appends `- <note>` to `MEMORY.md`. Edits
  take effect for agents built after the change (cached agents keep their old prompt until evicted,
  `/new`, or restart).
- **MEM-19** Files are plain text so they can be reviewed, edited by hand, and versioned in git.

## Choosing where information goes

| Information | Best layer | Why |
|---|---|---|
| "Always answer in Spanish" | core (`MEMORY.md`) | needed every turn |
| "User's project uses Postgres 16" | long-term | recalled when relevant |
| Last few questions | working/session | already in context |
| Task intermediate notes | task progress | scoped to the task |

## Acceptance criteria *(verified with a scripted model)*

- Re-created agent for the same session sees earlier messages.
- Duplicate `add` returns false; punctuation/quote-laden queries do not break search; another
  namespace returns nothing.
- A stored fact appears in the model input for a related question (injection).
- List/delete/clear are namespace-scoped; deleting removes the fact from search *(verified)*.
- Hybrid: retrieves a paraphrase with zero shared words; survives embedding outage; `reindex` heals; model change ignores
  old vectors; delete cascades *(verified with a fake embedder)*. The OpenAI-compatible request format *(verified with a mock
  transport)*; real embedding services *(unverified)*.
- Retention prunes old sessions and finished tasks but protects live-task sessions; an old-schema database is upgraded *(verified)*.
- Automatic extraction producing facts via a real model *(unverified; configured via Strands)*.
- Core-memory truncation behaviour *(by inspection)*.
