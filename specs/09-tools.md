# 09. Tools

Source: `tools/__init__.py`, `tools/builtin.py`, `tasks/tools.py`.

## Tool specification strings

An agent's `tools:` entries are resolved in this order (`resolve_tools`):

1. **TLS-1 Registered name** (e.g. `time`, `files`): the registered `ToolFactory(ctx: AppContext)` is
   called; it may return one tool, a module, or a list.
2. **TLS-2 `module:attr`** (e.g. `my_pkg.tools:search`, or Strands' own vended tools such as
   `strands.vended_tools:sleep` and `strands.vended_tools:shell`): imports the module and uses the attribute.
3. **TLS-3 Module path** (e.g. `my_pkg.my_tool`): imports the module; Strands treats a module with a
   `TOOL_SPEC` and a function of the same name as a classic module-style tool.

Note: the separate `strands-agents-tools` package (v0.9+) no longer ships tools; its tools moved into
Strands core as `strands.vended_tools`. This project does not depend on it. Vended `shell` runs commands
**unsandboxed** on the host: prefer the `sandbox` group and avoid vended `shell`/`python_repl` on agents
that face untrusted users.

Resolution errors (unknown module, missing attribute) raise at agent construction.

## Built-in tool groups

| Name | Tools | Notes |
|---|---|---|
| `time` | `current_time()` | UTC ISO-8601 |
| `files` | `list_files(subdir)`, `read_file(path)`, `write_file(path, content)` | sandboxed to `workspace/files` |
| `core_memory` | `remember_core(note)` | appends to `MEMORY.md` |
| `tasks` | `start_task`, `list_tasks`, `task_status`, `cancel_task` | see spec 07 |
| `schedule` | `remind_me`, `schedule_job`, `list_schedules`, `cancel_schedule` | see spec 14 |
| `web` | `web_fetch`, `web_search` (only if a search backend is configured) | SSRF-guarded; see spec 16 |
| `sandbox` | `run_shell`, `run_python` | isolated execution; fails closed; pair with `require_approval`; see specs 15, 16 |

Automatically added (not listed in `tools:`): `search_memory` (all agents), `add_memory` (persistent
top-level agents), `ask_<delegate>`, `report_progress` (task agents).

## File sandbox (TLS-4)

> Per-agent: the `files` and `sandbox` tools operate in `ctx.files_root`, which is the shared `files/` folder, or
> `files/agents/<name>/` for an agent with `files: private` / `isolated: true` (spec 08). Isolated agents cannot read the
> shared folder or escape their own.

- Paths are resolved against `workspace/files`; any path whose resolved location is outside that
  directory (via `..` or symlinks) raises `PermissionError`.
- `list_files` returns at most 500 entries, hiding dot-files; `read_file` returns at most 50,000
  characters and replaces undecodable bytes; `write_file` creates parent directories.

## Writing a tool

```python
from strands import tool
from kiro_crew.tools import tool_factories

@tool_factories.register("weather")
def weather(ctx):                       # ctx gives config, db, workspace, models, tasks
    @tool
    def get_weather(city: str) -> str:
        """Current weather for a city.

        Args:
            city: City name.
        """
        ...
    return get_weather
```

Use `@tool(context=True)` and a `ToolContext` parameter to read `tool_context.agent.state`
(`user_id`, `agent_name`, `origin`).

## Guidance

- **TLS-5** Docstrings with `Args:` sections become the tool schema shown to the model; keep them accurate.
- **TLS-6** Tools run with the process's authority. Prefer narrow tools and keep powerful ones
  (shell, network, cloud mutation) off agents that face untrusted users.

## Acceptance criteria

- Registered names resolve and register tools on the agent *(verified for `time`, `tasks`)*.
- File sandbox rejects `..`, absolute paths and symlinks pointing outside `files/`; `write_file` creates parent directories *(verified)*.
- `module:attr` (including `strands.vended_tools:sleep`) and classic module-path resolution *(verified)*.
