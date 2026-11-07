"""Which agent answers this message? Pure decision logic (no I/O), so it is trivially testable.

Precedence, highest first:

1. a per-conversation override the user set with ``/agent <name>`` (persisted),
2. the first matching ``routes:`` rule,
3. the channel's own ``agent:``,
4. the global ``entry_agent``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core.config import ChannelConfig, Config, RouteRule
from .core.types import InboundMessage


@dataclass(frozen=True)
class Route:
    agent: str
    reason: str  # "override" | "route[<i>]" | "channel" | "entry" -- shown in debug logs and /agent


def matches(rule: RouteRule, msg: InboundMessage) -> bool:
    """Every field the rule specifies must match; unspecified fields match anything."""
    return ((rule.channel is None or rule.channel == msg.address.channel)
            and (rule.conversation is None or rule.conversation == msg.address.conversation_id)
            and (rule.user is None or rule.user == msg.user_id)
            and (rule.group is None or rule.group == msg.is_group))


def resolve_agent(cfg: Config, channel: ChannelConfig, msg: InboundMessage, override: str | None = None) -> Route:
    if override is not None and override in cfg.agents:  # a stale override (agent removed on reload) is ignored
        return Route(override, "override")
    for i, rule in enumerate(cfg.routes):
        if matches(rule, msg):
            return Route(rule.agent, f"route[{i}]")
    if channel.agent:
        return Route(channel.agent, "channel")
    return Route(cfg.entry_agent, "entry")


def switchable_agents(cfg: Config, channel: ChannelConfig, *, is_admin: bool) -> list[str]:
    """Agents a user may pick with ``/agent``: those marked ``selectable`` (narrowed by the channel's
    ``agents_allowed``). Admins may pick any agent."""
    if is_admin:
        return sorted(cfg.agents)
    names = [n for n, a in cfg.agents.items() if a.selectable]
    if channel.agents_allowed is not None:
        names = [n for n in names if n in channel.agents_allowed]
    return sorted(names)
