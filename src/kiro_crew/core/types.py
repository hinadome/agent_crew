"""Channel-agnostic message types. Channels translate to/from these; nothing else leaks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Address:
    """Where a conversation lives. Enough to route a reply (or a proactive task result) back."""

    channel: str  # "discord", "slack", ...
    conversation_id: str  # DM / channel / chat id
    thread_id: str | None = None

    @property
    def key(self) -> str:
        """Stable, filesystem-safe session key."""
        raw = f"{self.channel}:{self.conversation_id}" + (f":{self.thread_id}" if self.thread_id else "")
        return re.sub(r"[^A-Za-z0-9_.:@-]", "_", raw)[:120] + "-" + hashlib.sha1(raw.encode()).hexdigest()[:8]


@dataclass
class InboundMessage:
    address: Address
    user_id: str  # sender id on that channel
    text: str
    user_name: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)
    raw: Any = None  # original platform payload, for adapters that need it
    is_group: bool = False  # True for group / server / channel chats; False for direct messages


@dataclass
class OutboundMessage:
    address: Address
    text: str
    reply_to: str | None = None
