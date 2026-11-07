"""Agent management shared by the CLI, the chat commands and the dashboard: overview, scaffold, remove, validate."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import agentfiles
from ..core.config import Config, load_config

TEMPLATES: dict[str, dict[str, Any]] = {
    # name -> front-matter defaults. Prompts are supplied by the caller.
    "basic": {},
    "public": {"isolated": True, "selectable": True},  # low-trust, public-facing: no owner persona/memory
    "specialist": {"stateful": False},  # meant to be called by another agent (use --delegate-of)
}


def overview(cfg: Config) -> list[dict[str, Any]]:
    """One row per agent: what it is, where it came from, what reaches it. Safe to expose (no prompts/secrets)."""
    routed: dict[str, list[str]] = {}
    for c, ch in cfg.channels.items():
        if ch.agent:
            routed.setdefault(ch.agent, []).append(f"channel:{c}")
    for i, r in enumerate(cfg.routes):
        bits = [f"{k}={v}" for k, v in (("channel", r.channel), ("conversation", r.conversation), ("user", r.user),
                                        ("group", r.group)) if v is not None]
        routed.setdefault(r.agent, []).append(f"route[{i}]({', '.join(bits)})")
    parents: dict[str, list[str]] = {}
    for n, a in cfg.agents.items():
        for d in a.delegates:
            parents.setdefault(d, []).append(n)
    rows = []
    for name, a in cfg.agents.items():
        src = cfg.agent_sources.get(name, "yaml")
        rows.append({
            "name": name, "description": a.description, "source": "yaml" if src == "yaml" else "file",
            "path": None if src == "yaml" else src, "model": a.model or f"{cfg.models.default} (default)",
            "tools": list(a.tools), "mcp_servers": list(a.mcp_servers), "delegates": list(a.delegates),
            "delegate_of": sorted(parents.get(name, [])), "isolated": a.isolated, "soul": a.use_soul,
            "core_memory": a.use_core_memory, "private_files": a.private_files, "private_memory": a.private_memory,
            "selectable": a.selectable, "stateful": a.stateful, "require_approval": list(a.require_approval),
            "routed_from": routed.get(name, []) + (["entry_agent"] if name == cfg.entry_agent else []),
        })
    return rows


@dataclass
class Validation:
    ok: bool
    message: str
    agents: int = 0
    from_files: int = 0
    warnings: list[str] = field(default_factory=list)


def validate(config_path: str | Path) -> Validation:
    """Load the whole configuration (YAML + drop-in files) exactly as the app would, and report the outcome."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            cfg = load_config(config_path)
        except Exception as e:  # noqa: BLE001  (any config problem is reported, never raised, to the user)
            return Validation(False, str(e))
    files = sum(1 for s in cfg.agent_sources.values() if s != "yaml")
    return Validation(True, f"OK: {len(cfg.agents)} agents ({len(cfg.agents) - files} in YAML, {files} drop-in files)",
                      len(cfg.agents), files, [str(w.message) for w in caught])


def scaffold(config_path: str | Path, name: str, *, prompt: str, description: str = "", model: str | None = None,
             tools: list[str] | None = None, delegate_of: list[str] | None = None, isolated: bool | None = None,
             selectable: bool | None = None, stateful: bool | None = None, template: str = "basic") -> Path:
    """Create ``<agents dir>/<name>.md`` and prove the resulting configuration loads; roll back if it does not."""
    if not agentfiles.AGENT_NAME.match(name):
        raise ValueError(f"invalid agent name {name!r}: {agentfiles.NAME_RULE}")
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; choose from {', '.join(TEMPLATES)}")
    cfg = load_config(config_path)
    if name in cfg.agents:
        where = cfg.agent_sources.get(name, "yaml")
        raise ValueError(f"agent {name!r} already exists ({'in the YAML config' if where == 'yaml' else where})")
    meta: dict[str, Any] = dict(TEMPLATES[template])
    for key, val in (("description", description), ("model", model), ("tools", tools), ("delegate_of", delegate_of),
                     ("isolated", isolated), ("selectable", selectable), ("stateful", stateful)):
        if val not in (None, "", []):
            meta[key] = val
    path = cfg.agents_dir / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(agentfiles.render_agent_file(meta, prompt or "You are a helpful assistant."), encoding="utf-8")
    result = validate(config_path)
    if not result.ok:
        path.unlink(missing_ok=True)  # never leave a file that breaks the running bot's next reload
        raise ValueError(f"not created: {result.message}")
    return path


def remove(config_path: str | Path, name: str) -> Path:
    """Delete a drop-in agent file. Agents defined in the YAML must be removed by editing that file."""
    cfg = load_config(config_path)
    src = cfg.agent_sources.get(name)
    if src is None:
        raise ValueError(f"no such agent {name!r}")
    if src == "yaml":
        raise ValueError(f"{name!r} is defined in the YAML config; remove it there (drop-in files are removed here)")
    path = Path(src)
    backup = path.read_text(encoding="utf-8")
    path.unlink()
    result = validate(config_path)
    if not result.ok:  # something still references it: restore and explain
        path.write_text(backup, encoding="utf-8")
        raise ValueError(f"not removed (still referenced): {result.message}")
    return path
