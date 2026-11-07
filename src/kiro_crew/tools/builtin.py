"""Built-in tools. Each is registered by name so YAML can opt in per agent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from strands import tool

from ..core.context import AppContext
from . import tool_factories


@tool_factories.register("time")
def _time(ctx: AppContext) -> Any:
    @tool
    def current_time() -> str:
        """Get the current date and time in UTC (ISO 8601)."""
        return datetime.now(UTC).isoformat(timespec="seconds")

    return current_time


@tool_factories.register("files")
def _files(ctx: AppContext) -> Any:
    ws = ctx.workspace
    root = ctx.files_root  # shared files/, or files/agents/<name>/ for isolated agents

    @tool
    def list_files(subdir: str = ".") -> str:
        """List files in the agent's sandboxed workspace folder.

        Args:
            subdir: Sub-directory relative to the sandbox root.
        """
        base = ws.resolve(subdir, root)
        if not base.is_dir():
            return f"Not a directory: {subdir}"
        return "\n".join(sorted(str(p.relative_to(root)) + ("/" if p.is_dir() else "")
                                for p in base.rglob("*") if p.name[0] != ".")[:500]) or "(empty)"

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file from the sandboxed workspace folder (first 50k chars).

        Args:
            path: File path relative to the sandbox root.
        """
        return ws.resolve(path, root).read_text(errors="replace")[:50_000]

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a text file in the sandboxed workspace folder.

        Args:
            path: File path relative to the sandbox root.
            content: Full file contents.
        """
        p = ws.resolve(path, root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} chars to {path}"

    return [list_files, read_file, write_file]


@tool_factories.register("core_memory")
def _core_memory(ctx: AppContext) -> Any:
    @tool
    def remember_core(note: str) -> str:
        """Append a short, durable note to CORE memory, which is shown in every future conversation.
        Use sparingly for the most important standing facts (names, preferences, rules). For
        everything else use the searchable long-term memory.

        Args:
            note: One concise sentence.
        """
        ctx.workspace.append_core_memory(note)
        return "Saved to core memory."

    return remember_core
