"""Central logging: levels, per-area overrides, file/JSON output, per-turn context, and a live ring buffer.

One call to :func:`configure` wires everything, so visibility is identical whether the app is launched by
``kiro-crew run`` or embedded. Every log record is enriched with the current turn's identifiers (via
``contextvars``), so you can follow one conversation across the gateway, agent, tools, memory and tasks.

The in-process :class:`RingBuffer` keeps the most recent records and feeds live subscribers (the web UI).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import re
import sys
import time
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ---- per-turn context --------------------------------------------------------------------------
# Set at the edges (gateway turn, task run, scheduler fire); every log line inherits it.


@dataclass
class TurnContext:
    turn_id: str
    kind: str = "chat"  # chat | task | schedule | heartbeat | delegate
    channel: str | None = None
    conversation: str | None = None
    user: str | None = None
    agent: str | None = None


_CURRENT: contextvars.ContextVar[TurnContext | None] = contextvars.ContextVar("kiro_turn", default=None)
_seq = 0


def _next_turn_id() -> str:
    global _seq
    _seq += 1
    return f"t{int(time.time())}-{_seq}"


def start_turn(kind: str = "chat", **fields: Any) -> contextvars.Token:
    """Begin a traced unit of work. Returns a token to pass to :func:`end_turn`."""
    return _CURRENT.set(TurnContext(turn_id=_next_turn_id(), kind=kind,
                                    **{k: v for k, v in fields.items() if v is not None}))


def end_turn(token: contextvars.Token) -> None:
    _CURRENT.reset(token)


def current_turn() -> TurnContext | None:
    return _CURRENT.get()


# ---- live ring buffer --------------------------------------------------------------------------


@dataclass
class LogEvent:
    ts: float
    level: str
    logger: str
    message: str
    turn_id: str | None = None
    kind: str | None = None
    channel: str | None = None
    conversation: str | None = None
    user: str | None = None
    agent: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RingBuffer(logging.Handler):
    """Keeps the last ``capacity`` events and fans each new event out to live async subscribers."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._events: deque[LogEvent] = deque(maxlen=capacity)
        self._subscribers: set[asyncio.Queue[LogEvent]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        try:
            event = _record_to_event(record)
        except Exception:  # never let logging raise  # noqa: BLE001
            return
        self._events.append(event)
        if self._loop is None or not self._subscribers:
            return
        # emit() may run on a worker thread; hop to the loop to touch the asyncio queues safely.
        self._loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: LogEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a slow viewer drops events rather than blocking logging

    def recent(self, limit: int = 500, min_level: int = 0) -> list[LogEvent]:
        items = [e for e in self._events if logging.getLevelName(e.level) >= min_level] if min_level else list(
            self._events)
        return items[-limit:]

    def subscribe(self) -> asyncio.Queue[LogEvent]:
        q: asyncio.Queue[LogEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[LogEvent]) -> None:
        self._subscribers.discard(q)


_RING: RingBuffer | None = None


def ring() -> RingBuffer | None:
    return _RING


# ---- redaction ---------------------------------------------------------------------------------
# Logs and the web dashboard must be safe to turn on. Two independent protections:
#   1. CONTENT hiding (default ON): message text, tool arguments/results, commands and code are replaced by a
#      length placeholder. Opt in to see them with `include_content`.
#   2. SECRET scrubbing (default ON, always applied to every line from any logger): known token shapes,
#      `key=value` credentials, Bearer headers, and the exact secret values found in the config.
# Both are applied centrally by a handler filter, so third-party and future log calls are covered too.

CONTENT_FIELDS = frozenset({"text", "args", "result", "argv", "command", "code", "content", "prompt", "body",
                            "output", "stdout", "stderr", "query"})

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"), "[REDACTED:api-key]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\b[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}\b"), "[REDACTED:discord-token]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"), "[REDACTED:telegram-token]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "[REDACTED:github-token]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED:jwt]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"),
    # key=value / key: value / "key": "value"  (api_key, secret, token, password, authorization ...)
    (re.compile(r"(?i)\b((?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|token|password|"
                r"passwd|authorization|app[_-]?secret|verify[_-]?token)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&)}\]]{4,})"),
     r"\1[REDACTED]"),
]


@dataclass
class RedactionPolicy:
    include_content: bool = False
    redact_secrets: bool = True
    secrets: frozenset[str] = frozenset()
    explicit: bool = False  # set when the CLI chose include_content, so config does not override it


_POLICY = RedactionPolicy()

# Third-party loggers that dump request/response bodies or headers at DEBUG. Held at WARNING unless content
# logging is explicitly enabled (an explicit area override still wins).
_NOISY_THIRD_PARTY = ("httpx", "httpcore", "openai", "anthropic", "litellm", "LiteLLM", "botocore", "boto3",
                      "urllib3", "aiohttp.access", "slack_bolt", "slack_sdk", "discord", "hpack", "mcp")


def policy() -> RedactionPolicy:
    return _POLICY


_CONTENT_LEAKY_PREFIXES = ("strands",) + _NOISY_THIRD_PARTY  # loggers whose DEBUG lines embed message content


def _clamped(name: str, level: int) -> int:
    """While content hiding is on, loggers that dump message bodies (Strands logs the memory-search query and the
    full tool_use dict at DEBUG) are held at WARNING no matter what was requested. ``include_content`` lifts it."""
    if _POLICY.include_content or level >= logging.WARNING:
        return level
    root = name.split(".")[0]
    if root in _CONTENT_LEAKY_PREFIXES or name in _CONTENT_LEAKY_PREFIXES:
        return logging.WARNING
    return level


def set_known_secrets(values: Iterable[str]) -> None:
    """Register exact secret strings (from config) to be masked wherever they appear."""
    _POLICY.secrets = frozenset(v for v in values if isinstance(v, str) and len(v) >= 6)


_SECRET_KEYS = ("token", "secret", "api_key", "apikey", "password", "passwd", "access_key", "authorization")


def collect_secrets(obj: Any) -> set[str]:
    """Walk a config-like structure (dicts/lists/pydantic models) and gather string values under secret-ish keys."""
    found: set[str] = set()

    def walk(node: Any, key_hint: str = "") -> None:
        if hasattr(node, "model_dump"):
            node = node.model_dump(by_alias=False)
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k).lower())
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key_hint)
        elif isinstance(node, str) and any(m in key_hint for m in _SECRET_KEYS) and len(node) >= 6:
            found.add(node)

    walk(obj)
    return found


def scrub(text: str) -> str:
    """Mask secrets in ``text`` (known literal values first, then token-shaped patterns)."""
    if not _POLICY.redact_secrets or not text:
        return text
    for secret in _POLICY.secrets:
        if secret in text:
            text = text.replace(secret, "[REDACTED]")
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def scrub_obj(value: Any) -> Any:
    """Recursively scrub strings inside containers, preserving structure (for JSON/UI consumers)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_obj(v) for v in value) if not isinstance(value, tuple) else tuple(
            scrub_obj(v) for v in value)
    return value


def _hide(value: Any) -> str:
    n = len(value) if isinstance(value, (str, bytes, list, tuple, dict)) else len(repr(value))
    return f"<{n} chars hidden>"


def apply_content_policy(include_content: bool, explicit: bool = False) -> None:
    _POLICY.include_content = include_content
    _POLICY.explicit = explicit or _POLICY.explicit
    for name in _NOISY_THIRD_PARTY:
        lg = logging.getLogger(name)
        if include_content:
            lg.setLevel(logging.NOTSET)
        elif lg.level == logging.NOTSET or lg.level < logging.WARNING:
            lg.setLevel(logging.WARNING)
    strands = logging.getLogger("strands")
    if include_content:
        base = logging.getLogger("kiro_crew").level
        strands.setLevel(base if 0 < base <= logging.DEBUG else logging.WARNING)
    elif strands.level < logging.WARNING:
        strands.setLevel(logging.WARNING)


class _RedactFilter(logging.Filter):
    """Scrubs secrets from the formatted message and any string extras, for every handler we install."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _POLICY.redact_secrets:
            return True
        with contextlib.suppress(Exception):  # logging must never raise (and must not log from inside a filter)
            msg = record.getMessage()
            clean = scrub(msg)
            if clean != msg or record.args:
                record.msg, record.args = clean, ()  # freeze the scrubbed text for every later handler
            for k, v in list(vars(record).items()):
                if k in _RESERVED or k.startswith("_") or k == "turn":
                    continue
                if isinstance(v, (str, dict, list, tuple)):
                    setattr(record, k, scrub_obj(v))
            # Tracebacks are rendered by the Formatter *after* filters run. Pre-render and scrub them here
            # (Formatter reuses a cached ``exc_text``), so a secret inside an exception message cannot leak.
            if record.exc_info and not record.exc_text:
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = scrub(record.exc_text)
        return True


# ---- formatting --------------------------------------------------------------------------------

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
# standard LogRecord attributes (plus ones added during formatting) to skip when harvesting extras


def _plain(v: Any) -> Any:
    """JSON-safe copy of a log field. Third-party libraries attach arbitrary objects to records; a single one
    must never break the JSON log file or the dashboard API."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return str(v)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: _plain(v) for k, v in vars(record).items()
            if k not in _RESERVED and not k.startswith("_") and k != "turn"}


def _record_to_event(record: logging.LogRecord) -> LogEvent:
    turn: TurnContext | None = getattr(record, "turn", None) or current_turn()
    return LogEvent(
        ts=record.created, level=record.levelname, logger=record.name, message=record.getMessage(),
        turn_id=turn.turn_id if turn else None, kind=turn.kind if turn else None,
        channel=turn.channel if turn else None, conversation=turn.conversation if turn else None,
        user=turn.user if turn else None, agent=turn.agent if turn else None,
        fields=_extra_fields(record))


class _TurnFilter(logging.Filter):
    """Attaches the current TurnContext to each record so handlers and formatters can show it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "turn"):
            record.turn = current_turn()
        return True


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        turn: TurnContext | None = getattr(record, "turn", None)
        if turn:
            tag = f"[{turn.turn_id} {turn.kind}" + (f" {turn.channel}:{turn.conversation}" if turn.conversation else "")
            tag += (f" user={turn.user}" if turn.user else "") + (f" agent={turn.agent}" if turn.agent else "") + "]"
            return f"{base} {tag}"
        return base


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_to_event(record).as_dict(), ensure_ascii=False, default=str)


# ---- configuration -----------------------------------------------------------------------------

_AREA_LOGGERS = {  # friendly names -> logger prefixes, for --log area=level and config
    "gateway": "kiro_crew.gateway",
    "agents": "kiro_crew.agents",
    "tasks": "kiro_crew.tasks",
    "scheduler": "kiro_crew.scheduler",
    "memory": "kiro_crew.memory",
    "tools": "kiro_crew.tools",
    "channels": "kiro_crew.channels",
    "security": "kiro_crew.security",
    "web_ui": "kiro_crew.web",
    "strands": "strands",
}


def _coerce_level(value: str | int) -> int:
    if isinstance(value, int):
        return value
    lvl = logging.getLevelName(str(value).upper())
    if not isinstance(lvl, int):
        raise ValueError(f"Unknown log level {value!r}")  # noqa: TRY004 (ValueError is the right API error here)
    return lvl


def configure(
    level: str | int = "INFO",
    *,
    areas: Mapping[str, str | int] | None = None,
    file: str | Path | None = None,
    json_format: bool = False,
    console: bool = True,
    ring_capacity: int = 2000,
    strands_level: str | int | None = None,
    quiet_console: bool = False,
    include_content: bool | None = None,
    redact_secrets: bool = True,
    secrets: Iterable[str] = (),
) -> RingBuffer:
    """Install handlers on the root logger. Idempotent: replaces any handlers we previously added.

    Args:
        level: base level for ``kiro_crew`` (and the root).
        areas: per-area overrides, keys from ``_AREA_LOGGERS`` or raw logger names.
        file: if set, also write (rotating) logs here.
        json_format: emit JSON lines instead of human text.
        console: write to stderr.
        ring_capacity: size of the in-memory buffer feeding the web UI.
        strands_level: level for Strands' own logger; defaults to WARNING unless base is DEBUG.
        quiet_console: keep the console at INFO even when base level is lower (so a terminal chat stays
            readable while the file/ring capture DEBUG).
        include_content: log message text / tool args / results / commands. Default (None/False) hides them
            behind length placeholders. ``None`` leaves a previously explicit choice untouched.
        redact_secrets: mask token-shaped strings and the exact ``secrets`` in every line, from every logger.
        secrets: exact secret values (usually from the config) to mask.
    """
    global _RING
    root = logging.getLogger()
    base = _coerce_level(level)
    area_levels = {k: _coerce_level(v) for k, v in (areas or {}).items()}
    # Handlers must pass the lowest level any logger is set to, else a per-area DEBUG override would be
    # filtered at the handler even though its logger allows it.
    effective = min([base, *area_levels.values()]) if area_levels else base
    root.setLevel(min(effective, logging.INFO))

    for h in [h for h in root.handlers if getattr(h, "_kiro", False)]:
        root.removeHandler(h)

    _POLICY.redact_secrets = redact_secrets
    set_known_secrets(secrets)
    if include_content is not None:
        apply_content_policy(include_content, explicit=True)
    else:
        apply_content_policy(_POLICY.include_content)

    turn_filter = _TurnFilter()
    redact_filter = _RedactFilter()
    human = _HumanFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fmt: logging.Formatter = _JsonFormatter() if json_format else human

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.INFO if quiet_console and effective < logging.INFO else effective)
        ch.setFormatter(fmt)
        ch.addFilter(turn_filter)
        ch.addFilter(redact_filter)
        ch._kiro = True  # type: ignore[attr-defined]
        root.addHandler(ch)

    if file:
        path = Path(file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(effective)
        fh.setFormatter(_JsonFormatter() if json_format else human)
        fh.addFilter(turn_filter)
        fh.addFilter(redact_filter)
        fh._kiro = True  # type: ignore[attr-defined]
        root.addHandler(fh)

    _RING = RingBuffer(ring_capacity)
    _RING.setLevel(effective)
    _RING.addFilter(turn_filter)
    _RING.addFilter(redact_filter)
    _RING._kiro = True  # type: ignore[attr-defined]
    root.addHandler(_RING)

    logging.getLogger("kiro_crew").setLevel(base)
    # Strands: quiet by default so it does not drown app logs, unless explicitly raised.
    s_level = _coerce_level(strands_level) if strands_level is not None else (
        base if base <= logging.DEBUG else logging.WARNING)
    capped = _clamped("strands", s_level) != s_level
    logging.getLogger("strands").setLevel(_clamped("strands", s_level))

    for area, lvl in area_levels.items():
        target = _AREA_LOGGERS.get(area, area)
        if _clamped(target, lvl) != lvl:
            capped = True
        logging.getLogger(target).setLevel(_clamped(target, lvl))
    if capped and base <= logging.DEBUG:
        logging.getLogger("kiro_crew.logging").info(
            "Third-party DEBUG logs (strands, httpx, ...) are held at WARNING because they print message "
            "content; pass --log-content / logging.include_content to see them")

    return _RING


def parse_area_overrides(items: list[str] | None) -> dict[str, str]:
    """Parse ``--log gateway=debug --log memory=info`` into a dict."""
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--log expects area=level, got {item!r}")
        area, lvl = item.split("=", 1)
        out[area.strip()] = lvl.strip()
    return out


def area_names() -> list[str]:
    return list(_AREA_LOGGERS)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Structured log helper: ``log(logger, logging.DEBUG, "tool call", tool=name, args=...)``.

    Extra fields land in the JSON output and the web UI, and are appended to the human console line.
    """
    if not logger.isEnabledFor(level):
        return
    if fields:
        if not _POLICY.include_content:
            fields = {k: (_hide(v) if k in CONTENT_FIELDS else v) for k, v in fields.items()}
        extras = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
        # LogRecord reserves some attribute names (name, msg, args, module, ...); a field colliding with
        # one would make ``logging`` raise. Rename such keys so the data still reaches handlers/the UI.
        safe = {(f"f_{k}" if k in _RESERVED else k): v for k, v in fields.items()}
        logger.log(level, "%s | %s", msg, extras, extra=safe)
    else:
        logger.log(level, msg)


def _short(value: Any, limit: int = 200) -> str:
    s = value if isinstance(value, str) else repr(value)
    return s if len(s) <= limit else s[:limit] + "..."


def iter_recent(limit: int = 500, min_level: str | int = 0) -> Iterator[dict[str, Any]]:
    r = _RING
    if r is None:
        return iter(())
    lvl = _coerce_level(min_level) if min_level else 0
    return (e.as_dict() for e in r.recent(limit, lvl))
