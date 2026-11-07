"""Drop-in agent files: ``<workspace>/agents/<name>.md``.

A file is YAML front matter (the agent's settings) followed by the system prompt as the markdown body::

    ---
    description: Handles customer questions
    model: claude
    tools: [web]
    isolated: true            # no owner persona / core memory, private files + memory
    delegate_of: [main]       # appear as an `ask_support` tool on the `main` agent (no YAML edit needed)
    selectable: true          # users may switch to it with /agent support
    ---
    You are a polite support agent. ...

The file name (without ``.md``) is the agent's name. Files starting with ``_`` or ``.`` are ignored, so
``_draft.md`` is a safe way to park an agent. This module only parses; validation lives with the config model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

AGENT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
NAME_RULE = "start with a letter, then letters/digits/_/-, at most 32 characters"


class AgentFileError(ValueError):
    """A problem in one agent file; the message always names the file."""


def split_front_matter(text: str, source: str = "<text>") -> tuple[dict[str, Any], str]:
    """Split ``---\\nyaml\\n---\\nbody``. A file with no front matter is all prompt."""
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text.strip()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise AgentFileError(f"{source}: front matter starts with '---' but is never closed") from None
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as e:
        raise AgentFileError(f"{source}: invalid YAML in front matter: {e}") from None
    if not isinstance(meta, dict):
        raise AgentFileError(f"{source}: front matter must be a mapping of settings")
    return meta, "\n".join(lines[end + 1:]).strip()


def agent_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if not p.name.startswith(("_", ".")))


def read_agent_file(path: Path, expand: Any = lambda v: v) -> tuple[str, dict[str, Any]]:
    """Return ``(name, settings)`` for one file. ``expand`` applies ``${ENV}`` expansion to the front matter only
    (the prompt body is left untouched because prompts may legitimately contain ``${...}``)."""
    name = path.stem
    if not AGENT_NAME.match(name):
        raise AgentFileError(f"{path}: file name {name!r} is not a valid agent name ({NAME_RULE})")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AgentFileError(f"{path}: cannot read file: {e}") from None
    meta, body = split_front_matter(text, str(path))
    meta = expand(meta)
    if body and "system_prompt" in meta:
        raise AgentFileError(f"{path}: has both a markdown body and a 'system_prompt' setting; use one")
    if body:
        meta["system_prompt"] = body
    return name, meta


def render_agent_file(settings: dict[str, Any], prompt: str) -> str:
    """Inverse of :func:`read_agent_file` (used by ``kiro-crew agent add``)."""
    front = yaml.safe_dump(settings, sort_keys=False, allow_unicode=True).strip() if settings else ""
    head = f"---\n{front}\n---\n" if front else ""
    return f"{head}{prompt.strip()}\n"
