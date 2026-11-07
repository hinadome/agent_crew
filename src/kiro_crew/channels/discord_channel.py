"""Discord (discord.py). Config: ``token``. Replies in DMs always; in servers only when @mentioned.

Enable the *Message Content* privileged intent for the bot in the Discord developer portal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import discord

from ..core.types import Address, InboundMessage, OutboundMessage
from . import Channel, MessageHandler, channel_registry

logger = logging.getLogger(__name__)


def discord_inbound(m: Any, me: Any) -> InboundMessage | None:
    """discord.Message -> InboundMessage, or None to ignore. DMs always; server channels only when mentioned."""
    if me is None or m.author.bot or m.author == me:  # not logged in yet, or our own / another bot's message
        return None
    is_dm = m.guild is None
    if not is_dm and me not in m.mentions:
        return None
    uid = me.id
    text = m.content.replace(f"<@{uid}>", "").replace(f"<@!{uid}>", "").strip()
    if not text:
        return None
    return InboundMessage(Address("discord", str(m.channel.id)), str(m.author.id), text,
                          m.author.display_name, raw=m, is_group=not is_dm)


@channel_registry.register("discord")
class DiscordChannel(Channel):
    max_len = 1900  # hard cap is 2000

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        if not self.extra.get("token"):
            raise ValueError("discord channel requires 'token'")
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self._task: asyncio.Task[None] | None = None

    async def start(self, handler: MessageHandler) -> None:
        token = self.extra["token"]

        @self.client.event
        async def on_ready() -> None:
            logger.info("Discord connected as %s", self.client.user)

        @self.client.event
        async def on_message(m: discord.Message) -> None:  # discord.py dispatches by this exact name
            if (msg := discord_inbound(m, self.client.user)) is not None:
                await handler(msg)

        self._task = asyncio.create_task(self.client.start(token), name="discord")

    async def stop(self) -> None:
        await self.client.close()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def send(self, msg: OutboundMessage) -> None:
        cid = int(msg.address.conversation_id)
        ch = self.client.get_channel(cid) or await self.client.fetch_channel(cid)
        await ch.send(msg.text)  # type: ignore[union-attr]

    async def typing(self, address: Address) -> None:
        ch = self.client.get_channel(int(address.conversation_id))
        if ch is not None:
            await ch.typing()  # type: ignore[union-attr]
