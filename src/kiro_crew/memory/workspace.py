"""Workspace: human-readable, git-friendly files that shape the agent (OpenClaw-style).

    <workspace>/
      SOUL.md      persona, tone, standing rules   (you edit)
      MEMORY.md    curated core memory              (you + the agent edit; size-capped)
      files/       sandbox the agent may read/write
      kiro-crew.db sessions, long-term memory, tasks

Core memory is injected into every system prompt, so it is deliberately small and capped.
Bulk recall lives in the searchable long-term store instead.
"""

from __future__ import annotations

from pathlib import Path

MAX_CORE_CHARS = 6000
_DEFAULT_SOUL = "# Soul\nBe concise, proactive and honest. Say when you are unsure.\n"


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files_dir = root / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.soul_path = root / "SOUL.md"
        self.memory_path = root / "MEMORY.md"
        if not self.soul_path.exists():
            self.soul_path.write_text(_DEFAULT_SOUL)
        if not self.memory_path.exists():
            self.memory_path.write_text("# Core memory\n")

    @property
    def db_path(self) -> Path:
        return self.root / "kiro-crew.db"

    def system_context(self, *, soul: bool = True, core_memory: bool = True) -> str:
        """The owner's persona and core memory for a system prompt. An isolated (public-facing) agent asks for
        neither and gets an empty string, so none of the owner's private notes reach its prompt."""
        parts = []
        if soul:
            parts.append(f"<soul>\n{self.soul_path.read_text()[:MAX_CORE_CHARS]}\n</soul>")
        if core_memory:
            mem = self.memory_path.read_text()
            if len(mem) > MAX_CORE_CHARS:  # keep the newest notes when over budget
                mem = "...(older notes truncated)\n" + mem[-MAX_CORE_CHARS:]
            parts.append(f"<core_memory>\n{mem}\n</core_memory>")
        return "\n".join(parts)

    def append_core_memory(self, note: str) -> None:
        with self.memory_path.open("a") as f:
            f.write(f"- {note.strip()}\n")

    def agent_files_dir(self, agent: str) -> Path:
        """Private sandbox folder for one agent (``files/agents/<name>/``)."""
        d = self.files_dir / "agents" / agent
        d.mkdir(parents=True, exist_ok=True)
        return d

    def resolve(self, relative: str, root: Path | None = None) -> Path:
        """Resolve a path inside a sandbox root (default ``files/``); raises if it would escape it."""
        base = (root or self.files_dir).resolve()
        p = (base / relative).resolve()
        if not p.is_relative_to(base):
            raise PermissionError(f"Path escapes workspace sandbox: {relative}")
        return p
