# 10. Extensibility

Source: `core/registry.py`, each package's `__init__.py`.

## Registry mechanism

`Registry[T]` maps string names to implementations for one extension kind.

- **EXT-1** Methods: `register(name)` (decorator), `add(name, obj)`, `get(name)`, `names()`, `in`.
- **EXT-2** `get` of an unknown name raises `KeyError` listing the available names.
- **EXT-3** Re-registering a name with a different object logs a warning and replaces the old one
  (so a plugin can deliberately override a built-in).
- **EXT-4** Entry points in group `kiro_crew.<kind>s` are loaded lazily on the first `get/names/in`.
  Built-ins win over entry points of the same name (`setdefault`). A plugin that fails to import is
  logged and skipped.

## Extension points

| Kind | Registry | Signature | Entry-point group |
|---|---|---|---|
| Provider | `providers.providers` | `(ModelProfile) -> strands Model` | `kiro_crew.providers` |
| Channel | `channels.channel_registry` | `class X(Channel)` | `kiro_crew.channels` |
| Tool | `tools.tool_factories` | `(AppContext) -> tool \| module \| list` | `kiro_crew.tools` |
| Memory backend | `memory.longterm.memory_backends` | `(Storage \| Database, namespace, **options) -> MemoryStore` | `kiro_crew.memory_backends` |
| Storage backend | `storage.storage_backends` | `(Config, Workspace) -> Storage` (see spec 19) | `kiro_crew.storage_backends` |

Additional, narrower registries:

| Kind | Registry | Signature |
|---|---|---|
| Embedder (semantic memory) | `memory.embeddings.embedders` | `class X(Embedder)` with `async embed(texts) -> vectors` |
| Search backend (`web_search`) | `tools.web.search_backends` | `class X(SearchBackend)` with `async search(query, n)` |
| Sandbox backend | `sandbox.sandbox_backends` | `class X(SandboxBackend)` with `check()` and `async run(argv, workdir)` |

Persistence (sessions, tasks, schedules, pairing, settings) is pluggable as a unit through the storage registry (spec 19).
Not yet registries: the conversation-manager strategy.

## Loading plugins

Three equivalent routes:

1. **In-repo module:** list it under `plugins:` in YAML; it is imported at startup and its decorators run.
2. **Installed package:** declare an entry point, no YAML change except referencing the new name.
3. **Programmatic:** import and call `registry.add(...)` before building `App`.

```toml
# plugin package pyproject.toml
[project.entry-points."kiro_crew.providers"]
my-llm = "my_pkg.llm:build"
[project.entry-points."kiro_crew.channels"]
matrix = "my_pkg.matrix:MatrixChannel"
```

## `AppContext` (what factories receive)

| Field | Use |
|---|---|
| `config` | the validated `Config` |
| `db` | shared `Database` |
| `workspace` | `Workspace` (core memory, sandbox) |
| `models` | `ModelFactory` |
| `tasks` | `TaskStore` |
| `services` | late-bound: `agent_factory`, `gateway`, `task_runner` |

## Example: new channel (Matrix-like)

```python
from kiro_crew.channels import Channel, channel_registry
from kiro_crew.core.types import Address, InboundMessage, OutboundMessage

@channel_registry.register("matrix")
class MatrixChannel(Channel):
    max_len = 4000
    async def start(self, handler):
        self.handler = handler
        # connect to the platform, spawn a background task, and for each event call:
        # await handler(InboundMessage(Address("matrix", room_id), sender_id, text))
    async def stop(self): ...
    async def send(self, msg: OutboundMessage):
        ...  # post msg.text to msg.address.conversation_id
```

Config:

```yaml
plugins: [my_pkg.matrix]
channels:
  matrix: {enabled: true, homeserver: "https://…", token: "${MATRIX_TOKEN}", allow_from: ["@me:server"]}
```

Adapter settings arrive in `self.extra` (every key besides `enabled`, `allow_from`, `agent`).
Authorisation, sessions, commands and chunking are inherited for free.

## Example: new memory backend

```python
from kiro_crew.memory.longterm import memory_backends

@memory_backends.register("pgvector")
def build(db, namespace, **options):
    return PgVectorStore(namespace=namespace, **options)   # must satisfy strands.memory.MemoryStore
```

`memory.backend: pgvector` then selects it; `extraction` is passed in as a keyword for the store to
expose as its `.extraction` attribute.

## Agents are extensible without code

An agent is configuration, not a class: add one as a YAML entry or a drop-in markdown file (spec 20) and it appears with its
tools, routing and delegation. Plugins add *capabilities* (tools, providers, channels); files add *roles*.

## Compatibility rules

- **EXT-5** Extension contracts follow semantic versioning of this project; breaking changes to
  `Channel`, `ProviderBuilder`, `ToolFactory`, `MemoryBackend` or `AppContext` require a major bump.
- **EXT-6** Plugins MUST NOT mutate shared registries other than their own registrations.

## Acceptance criteria

- Custom provider, channel (`fake`), and tool registered via decorator work end to end *(verified)*.
- Entry-point loading *(implemented; not exercised with an installed plugin)*.
- `plugins:` module import *(implemented; not exercised)*.
