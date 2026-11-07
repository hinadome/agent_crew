"""Parse human schedule strings into (kind, expression, next run).

Accepted forms (case-insensitive):
  in 30m | in 2 hours | in 1d          one-shot, relative
  at 2026-11-07 09:00 | at 2026-11-07T09:00:00+02:00    one-shot, absolute (timezone from config if none given)
  every 15m | every 2 hours            fixed interval (minimum 60 s)
  cron 0 9 * * 1-5 | 0 9 * * 1-5       standard 5-field cron, evaluated in the configured timezone
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

MIN_INTERVAL_S = 60
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DUR = re.compile(r"^(\d+)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)$")


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Parsed:
    kind: str  # at | every | cron
    expr: str  # at: epoch seconds; every: seconds; cron: the expression
    next_run: float


def parse_duration(text: str) -> int:
    m = _DUR.match(text.strip().lower())
    if not m:
        raise ScheduleError(f"Cannot read duration {text!r}. Use e.g. 30m, 2h, 1d.")
    return int(m.group(1)) * _UNITS[m.group(2)[0]]


def parse_when(text: str, tz: str = "UTC", now: float | None = None) -> Parsed:
    now = time.time() if now is None else now
    t = text.strip()
    low = t.lower()
    if low.startswith("in "):
        secs = parse_duration(low[3:])
        return Parsed("at", str(now + secs), now + secs)
    if low.startswith("every "):
        secs = parse_duration(low[6:])
        if secs < MIN_INTERVAL_S:
            raise ScheduleError(f"Interval must be at least {MIN_INTERVAL_S}s.")
        return Parsed("every", str(secs), now + secs)
    if low.startswith("at "):
        try:
            dt = datetime.fromisoformat(t[3:].strip())
        except ValueError:
            raise ScheduleError("Use 'at YYYY-MM-DD HH:MM' (or ISO 8601).") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        ts = dt.timestamp()
        if ts <= now:
            raise ScheduleError("That time is in the past.")
        return Parsed("at", str(ts), ts)
    expr = t[5:].strip() if low.startswith("cron") else t
    expr = expr.lstrip(":").strip()
    if len(expr.split()) == 5 and croniter.is_valid(expr):
        return Parsed("cron", expr, next_cron(expr, tz, now))
    raise ScheduleError("Unrecognised schedule. Examples: 'in 30m', 'at 2026-11-07 09:00', "
                        "'every 2h', 'cron 0 9 * * 1-5'.")


def next_cron(expr: str, tz: str, after: float) -> float:
    base = datetime.fromtimestamp(after, ZoneInfo(tz))
    return croniter(expr, base).get_next(float)


def next_run(kind: str, expr: str, tz: str, after: float) -> float | None:
    """Next fire time strictly after ``after``; ``None`` for a finished one-shot."""
    if kind == "at":
        return None
    if kind == "every":
        return after + int(expr)
    return next_cron(expr, tz, after)


def describe(kind: str, expr: str) -> str:
    if kind == "at":
        return "once at " + datetime.fromtimestamp(float(expr)).strftime("%Y-%m-%d %H:%M")
    if kind == "every":
        s = int(expr)
        return "every " + (f"{s // 86400}d" if s % 86400 == 0 else f"{s // 3600}h" if s % 3600 == 0
                           else f"{s // 60}m" if s % 60 == 0 else f"{s}s")
    return f"cron {expr}"
