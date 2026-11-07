"""Chat channels. A channel only translates platform events <-> InboundMessage/OutboundMessage.

Auth (allow-lists), sessions, agents and memory are the gateway's job, so a new platform is ~60 lines.
Adapters are imported lazily so optional dependencies (discord.py, slack-bolt, ...) are only needed
for the channels you actually enable.
"""

from __future__ import annotations

import asyncio
import importlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from ..core.config import ChannelConfig
from ..core.registry import Registry
from ..core.types import Address, InboundMessage, OutboundMessage

MessageHandler = Callable[[InboundMessage], Awaitable[None]]


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text for platforms with message length caps, preferring paragraph/line/word boundaries."""
    text = text.strip() or "(empty response)"
    out: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out


class Channel(ABC):
    max_len: int = 2000
    local: bool = False  # True = trusted local console; bypasses the allow-list

    def __init__(self, name: str, config: ChannelConfig) -> None:
        self.name = name
        self.config = config
        self.extra = config.model_extra or {}  # adapter-specific settings (token, port, ...)
        self.quit = asyncio.Event()  # a channel sets this to ask the app to shut down (e.g. CLI Ctrl-D)

    @abstractmethod
    async def start(self, on_message: MessageHandler) -> None:
        """Begin receiving. Must return promptly (spawn background tasks for long-running loops)."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Deliver ``msg`` (already split to ``max_len`` by the gateway)."""

    async def typing(self, address: Address) -> None:
        """Optional 'is typing...' indicator."""


channel_registry: Registry[type[Channel]] = Registry("channel")

_BUILTIN = {
    "cli": "cli",
    "discord": "discord_channel",
    "slack": "slack_channel",
    "telegram": "telegram_channel",
    "whatsapp": "whatsapp_channel",
}


def channel_class(kind: str) -> type[Channel]:
    if kind not in channel_registry and kind in _BUILTIN:
        importlib.import_module(f"{__name__}.{_BUILTIN[kind]}")
    return channel_registry.get(kind)
