"""Typed configuration. One YAML file drives everything; ``${ENV_VAR}`` is expanded."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from . import agentfiles


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelProfile(_Cfg):
    provider: str  # key in the provider registry: bedrock | anthropic | openai | ollama | litellm | ...
    model_id: str
    params: dict[str, Any] = Field(default_factory=dict)  # temperature, max_tokens, ...
    client: dict[str, Any] = Field(default_factory=dict)  # api_key, base_url, region_name, host, ...


class ModelsConfig(_Cfg):
    default: str
    profiles: dict[str, ModelProfile]
    # If set, the default model becomes an ordered-failover router across these profiles.
    fallback: list[str] = Field(default_factory=list)
    # Cheaper model for background chores (memory extraction, summarisation). Optional.
    aux: str | None = None


class MemoryConfig(_Cfg):
    backend: str = "sqlite"  # key in the memory-backend registry
    # "user": each channel sender has private memory. "shared": one memory for everyone (single-owner assistant).
    scope: str = "user"
    options: dict[str, Any] = Field(default_factory=dict)
    inject_max_entries: int = 5
    auto_extract: bool = True  # distil durable facts from conversations automatically
    extract_every_n_turns: int = 5
    allow_agent_writes: bool = True  # expose an add_memory tool to the agent


class ConversationConfig(_Cfg):
    strategy: str = "summarizing"  # summarizing | sliding_window
    window_size: int = 40
    preserve_recent_messages: int = 10
    summary_ratio: float = 0.3


class MCPServerConfig(_Cfg):
    name: str
    command: str | None = None  # stdio transport
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None  # streamable-http transport
    headers: dict[str, str] = Field(default_factory=dict)


class StorageConfig(_Cfg):
    # Registry key: "sqlite" (default, one local file) | "memory" (not persisted) | any registered backend.
    backend: str = "sqlite"
    options: dict[str, Any] = Field(default_factory=dict)


class LoggingConfig(_Cfg):
    level: str = "INFO"  # base level for kiro_crew.*
    # Per-area overrides: {gateway: debug, memory: info}. Areas: gateway, agents, tasks, scheduler,
    # memory, tools, channels, security, web_ui, strands (or any raw logger name).
    areas: dict[str, str] = Field(default_factory=dict)
    file: str | None = None  # also write (rotating) logs here
    json_lines: bool = Field(default=False, alias="json")  # emit JSON lines instead of human text
    console: bool = True
    quiet_console: bool = False  # keep console at INFO even at a lower base (file/UI still get DEBUG)
    ring_capacity: int = 2000  # events kept in memory for the web UI
    strands_level: str | None = None  # override Strands' own logger level
    # Log message text, tool arguments/results, commands and code. OFF by default: logs and the web dashboard
    # then show lengths/timings/tool names only. Turn on only on a machine/log sink you trust.
    include_content: bool = False
    # Mask token-shaped strings and the exact secrets in this config in every log line (any logger).
    redact_secrets: bool = True


class WebUIConfig(_Cfg):
    enabled: bool = False
    host: str = "127.0.0.1"  # bind locally by default; it is read-only but exposes conversation metadata
    port: int = 8787
    # Token required to view the dashboard (query ?token= or X-Auth-Token header). Strongly recommended.
    token: str | None = None


class SchedulerConfig(_Cfg):
    enabled: bool = True
    timezone: str = "UTC"  # IANA name; used to interpret cron expressions and "at <time>"
    tick_s: int = 15
    max_jobs_per_user: int = 50


class HeartbeatConfig(_Cfg):
    """Periodic proactive turn: the agent reads HEARTBEAT.md and only speaks if something needs attention."""

    enabled: bool = False
    every: str = "2h"
    target: str | None = None  # "<channel>:<conversation_id>", e.g. "discord:123456789"
    agent: str | None = None
    user: str = "shared"  # memory owner used for heartbeat turns
    ok_token: str = "HEARTBEAT_OK"  # a reply starting with this is swallowed (nothing to report)


class SandboxConfig(_Cfg):
    backend: str = "docker"  # docker | host (UNSAFE, explicit opt-in) | none
    image: str = "python:3.12-slim"
    network: bool = False  # allow outbound network inside the sandbox
    timeout_s: int = 60
    memory: str = "512m"
    cpus: str = "1"
    max_output_chars: int = 20000


class HousekeepingConfig(_Cfg):
    enabled: bool = True
    interval_h: int = 24
    session_retention_days: int = 90  # conversations idle longer than this are deleted (0 = keep forever)
    task_retention_days: int = 30  # finished tasks older than this are deleted (0 = keep forever)


class AgentConfig(_Cfg):
    description: str = ""
    model: str | None = None  # profile name; defaults to models.default
    system_prompt: str = "You are a helpful assistant."
    # Read the prompt from a file instead (relative to the config file). Resolved at load time.
    system_prompt_file: str | None = None
    tools: list[str] = Field(default_factory=list)  # builtin names or "pkg.module:attr"
    mcp_servers: list[str] = Field(default_factory=list)  # names from top-level mcp_servers
    delegates: list[str] = Field(default_factory=list)  # other agents exposed to this one as tools
    # Tool names that need the user's /approve in chat before they run (e.g. run_shell).
    require_approval: list[str] = Field(default_factory=list)

    # ---- isolation: what this agent may see of the owner's private state -------------------------
    # `isolated: true` is the one-switch way to build a public-facing / low-trust agent: it gets NO owner
    # persona (SOUL.md) or core memory (MEMORY.md), its own files/ subfolder and its own long-term memory.
    isolated: bool = False
    soul: bool | None = None  # inject SOUL.md? default: yes unless isolated
    core_memory: bool | None = None  # inject MEMORY.md (and allow remember_core)? default: yes unless isolated
    files: str | None = None  # "shared" | "private" sandbox folder; default: shared unless isolated
    memory: str | None = None  # long-term memory: "shared" | "private" per agent; default: shared unless isolated

    # ---- use as a delegate / being chosen by users ---------------------------------------------
    stateful: bool = False  # as a delegate, keep its own conversation across calls (default: fresh each call)
    selectable: bool = False  # users may switch to it with /agent <name> (admins can pick any agent)

    @field_validator("files", "memory")
    @classmethod
    def _shared_or_private(cls, v: str | None) -> str | None:
        if v is not None and v not in ("shared", "private"):
            raise ValueError("must be 'shared' or 'private'")
        return v

    # Effective values after applying the `isolated` default.
    @property
    def use_soul(self) -> bool:
        return (not self.isolated) if self.soul is None else self.soul

    @property
    def use_core_memory(self) -> bool:
        return (not self.isolated) if self.core_memory is None else self.core_memory

    @property
    def private_files(self) -> bool:
        return (self.isolated if self.files is None else self.files == "private")

    @property
    def private_memory(self) -> bool:
        return (self.isolated if self.memory is None else self.memory == "private")


class RouteRule(_Cfg):
    """First matching rule wins. All given fields must match; at least one is required."""

    agent: str
    channel: str | None = None  # channel name, e.g. "discord"
    conversation: str | None = None  # conversation id (DM / channel / chat id)
    user: str | None = None  # sender id
    group: bool | None = None  # true = group/server/channel chats only, false = direct messages only


class AgentFilesConfig(_Cfg):
    dir: str = "agents"  # relative to the workspace (or absolute)
    watch: bool = True  # pick up added/changed/removed agent files and the config automatically
    interval_s: float = 3.0


class ChannelConfig(_Cfg):
    model_config = ConfigDict(extra="allow")  # channel-specific keys (token, port, ...)
    enabled: bool = False
    # Only these sender ids may talk to the agent. Empty = deny everyone (safe default).
    allow_from: list[str] = Field(default_factory=list)
    # "allowlist": unknown senders are ignored. "pairing": unknown senders get a one-time code that an
    # admin approves (`kiro-crew pairing approve <code>` or `/pair approve <code>`).
    access: str = "allowlist"
    agent: str | None = None  # default agent for this channel (see also top-level `routes`)
    # Restrict which agents users may switch to with /agent on this channel (None = any `selectable` agent).
    agents_allowed: list[str] | None = None


class TasksConfig(_Cfg):
    enabled: bool = True
    workers: int = 2
    max_attempts: int = 3
    task_timeout_s: int = 3600
    agent: str | None = None  # agent used to execute tasks; defaults to the entry agent
    verify: bool = True  # check finished tasks that declared `done_when` against it (one cheap model call)
    verify_all: bool = False  # also judge tasks without `done_when` against their goal
    max_verify_rounds: int = 2  # how many times the agent is sent back to fix an unmet result


class LimitsConfig(_Cfg):
    """Guard rails against runaway agents. 0 disables a limit. Counted per chat turn / per task run."""
    chat_tool_calls: int = 40
    task_tool_calls: int = 200
    repeat_limit: int = 3  # the same tool with identical arguments this many times in a row is blocked
    grace_calls: int = 3  # blocked calls tolerated before the run is stopped hard


class Config(_Cfg):
    workspace: Path = Path("~/.kiro-crew")
    plugins: list[str] = Field(default_factory=list)
    entry_agent: str = "main"
    models: ModelsConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    housekeeping: HousekeepingConfig = Field(default_factory=HousekeepingConfig)
    # Per-tool-group settings, e.g. {web: {search: brave, api_key: ...}}
    tool_options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    approval_timeout_s: int = 300
    # Senders allowed to run admin chat commands (/pair ...): "<channel>:<sender_id>". The CLI is always admin.
    admins: list[str] = Field(default_factory=list)
    agents: dict[str, AgentConfig]
    agent_files: AgentFilesConfig = Field(default_factory=AgentFilesConfig)
    # Pick the agent per message. First match wins; otherwise the channel's `agent`, then `entry_agent`.
    routes: list[RouteRule] = Field(default_factory=list)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    # Link one person across channels so they share memory: {alice: ["discord:123", "slack:U0ABC"]}
    identities: dict[str, list[str]] = Field(default_factory=dict)

    _agent_sources: dict[str, str] = PrivateAttr(default_factory=dict)  # name -> "yaml" | file path

    @property
    def agent_sources(self) -> dict[str, str]:
        return self._agent_sources

    @property
    def agents_dir(self) -> Path:
        """Where drop-in agent files live (``<workspace>/agents`` unless configured)."""
        d = Path(self.agent_files.dir).expanduser()
        return d if d.is_absolute() else self.workspace / d

    def model_post_init(self, _ctx: Any) -> None:
        self.workspace = self.workspace.expanduser().resolve()
        import logging as _logging
        from zoneinfo import ZoneInfo

        ZoneInfo(self.scheduler.timezone)  # raises on an unknown timezone
        levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        for name, lvl in [("logging.level", self.logging.level), *(((f"logging.areas.{k}", v)
                          for k, v in self.logging.areas.items())),
                          *(([("logging.strands_level", self.logging.strands_level)]) if self.logging.strands_level else [])]:
            if str(lvl).upper() not in levels:
                raise ValueError(f"{name}={lvl!r} is not a valid log level (one of {sorted(levels)})")
        del _logging
        if self.web_ui.enabled and not self.web_ui.token:
            # not fatal, but warn loudly via a clear message at load so it is not missed
            import warnings
            warnings.warn("web_ui.enabled without web_ui.token: the dashboard will be UNAUTHENTICATED", stacklevel=2)
        for cname, c in self.channels.items():
            if c.access not in ("allowlist", "pairing"):
                raise ValueError(f"channels.{cname}.access must be 'allowlist' or 'pairing'")
        names = set(self.models.profiles)
        for ref in [self.models.default, *self.models.fallback, *([self.models.aux] if self.models.aux else [])]:
            if ref not in names:
                raise ValueError(f"models references unknown profile {ref!r}")
        self._validate_agents(names)


    def _validate_agents(self, profiles: set[str]) -> None:
        for aname in self.agents:
            if not agentfiles.AGENT_NAME.match(aname):
                raise ValueError(f"agent name {aname!r} is invalid: {agentfiles.NAME_RULE}. (Agent names become "
                                 f"tool names like ask_{aname}, which LLM providers restrict to letters, digits, _ and -)")
        if self.entry_agent not in self.agents:
            raise ValueError(f"entry_agent {self.entry_agent!r} not defined in agents")
        mcp = {m.name for m in self.mcp_servers}
        for aname, a in self.agents.items():
            if a.model and a.model not in profiles:
                raise ValueError(f"agent {aname!r} uses unknown model profile {a.model!r}")
            for d in a.delegates:
                if d not in self.agents:
                    raise ValueError(f"agent {aname!r} delegates to unknown agent {d!r}")
                if d == aname:
                    raise ValueError(f"agent {aname!r} cannot delegate to itself")
            for m in a.mcp_servers:
                if m not in mcp:
                    raise ValueError(f"agent {aname!r} uses unknown mcp server {m!r}")
            if "core_memory" in a.tools and not a.use_core_memory:
                raise ValueError(f"agent {aname!r} has the 'core_memory' tool but core memory is disabled for it "
                                 f"(isolated agents must not write the owner's MEMORY.md)")
        refs = [("tasks.agent", self.tasks.agent), ("heartbeat.agent", self.heartbeat.agent),
                *[(f"channels.{c}.agent", ch.agent) for c, ch in self.channels.items()],
                *[(f"routes[{i}].agent", r.agent) for i, r in enumerate(self.routes)]]
        for where, ref in refs:
            if ref is not None and ref not in self.agents:
                raise ValueError(f"{where} refers to unknown agent {ref!r}")
        for c, ch in self.channels.items():
            for ref in ch.agents_allowed or []:
                if ref not in self.agents:
                    raise ValueError(f"channels.{c}.agents_allowed refers to unknown agent {ref!r}")
        for i, r in enumerate(self.routes):
            if r.channel is None and r.conversation is None and r.user is None and r.group is None:
                raise ValueError(f"routes[{i}] has no match field (channel / conversation / user / group)")
        self._warn_delegation_cycles()

    def _warn_delegation_cycles(self) -> None:
        import warnings

        seen: set[tuple[str, ...]] = set()

        def walk(node: str, path: list[str]) -> None:
            for nxt in self.agents[node].delegates:
                if nxt in path:
                    cyc = (*path[path.index(nxt):], nxt)
                    key = tuple(sorted(set(cyc)))
                    if key not in seen:
                        seen.add(key)
                        warnings.warn(f"delegation cycle {' -> '.join(cyc)}: it is contained by the nesting limit "
                                      f"of 2 but is probably not what you want", stacklevel=2)
                else:
                    walk(nxt, [*path, nxt])

        for a in self.agents:
            walk(a, [a])


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _merge_agents(data: dict[str, Any], config_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Combine YAML-defined agents with drop-in ``agents/*.md`` files. Returns ``(agents, sources)``."""
    agents: dict[str, Any] = {k: dict(v or {}) for k, v in (data.get("agents") or {}).items()}
    sources: dict[str, str] = dict.fromkeys(agents, "yaml")

    for name, a in agents.items():  # system_prompt_file on YAML agents
        pf = a.pop("system_prompt_file", None)
        if pf:
            path = (config_dir / pf).expanduser()
            if not path.is_file():
                raise agentfiles.AgentFileError(f"agent {name!r}: system_prompt_file {str(path)!r} does not exist")
            if "system_prompt" in a:
                raise agentfiles.AgentFileError(f"agent {name!r}: use either system_prompt or system_prompt_file")
            a["system_prompt"] = path.read_text(encoding="utf-8").strip()

    ws = Path(data.get("workspace") or "~/.kiro-crew").expanduser().resolve()
    afc = data.get("agent_files") or {}
    directory = Path(afc.get("dir", "agents")).expanduser()
    directory = directory if directory.is_absolute() else ws / directory

    delegate_of: dict[str, list[str]] = {}
    for path in agentfiles.agent_files(directory):
        name, meta = agentfiles.read_agent_file(path, _expand)
        if name in agents:
            raise agentfiles.AgentFileError(f"agent {name!r} is defined both in the YAML config and in {path}; "
                                            f"keep one")
        parents = meta.pop("delegate_of", None) or []
        if isinstance(parents, str):
            parents = [parents]
        pf = meta.pop("system_prompt_file", None)
        if pf:
            pp = (path.parent / pf).expanduser()
            if not pp.is_file():
                raise agentfiles.AgentFileError(f"{path}: system_prompt_file {str(pp)!r} does not exist")
            meta["system_prompt"] = pp.read_text(encoding="utf-8").strip()
        try:
            AgentConfig.model_validate(meta)
        except ValueError as e:  # pydantic ValidationError is a ValueError
            raise agentfiles.AgentFileError(f"{path}: {e}") from None
        agents[name] = meta
        sources[name] = str(path)
        if parents:
            delegate_of[name] = list(parents)

    for child, parents in delegate_of.items():  # "I am a delegate of ..." without editing the parent
        for parent in parents:
            if parent not in agents:
                raise agentfiles.AgentFileError(f"{sources[child]}: delegate_of refers to unknown agent {parent!r}")
            dl = agents[parent].setdefault("delegates", [])
            if child not in dl:
                dl.append(child)
    return agents, sources


def load_config(path: str | Path) -> Config:
    p = Path(path).expanduser()
    data = _expand(yaml.safe_load(p.read_text()) or {})
    agents, sources = _merge_agents(data, p.resolve().parent)
    data["agents"] = agents
    cfg = Config.model_validate(data)
    cfg._agent_sources = sources
    return cfg
