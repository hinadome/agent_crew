"""Slack via Socket Mode (no public URL needed). Config: ``bot_token`` (xoxb-), ``app_token`` (xapp-).

Subscribe the app to ``message.im`` and ``app_mention`` events. Replies stay in-thread for channels.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from ..core.types import Address, InboundMessage, OutboundMessage
from . import Channel, MessageHandler, channel_registry

logger = logging.getLogger(__name__)
_MENTION = re.compile(r"<@[UW][A-Z0-9]+>")


def slack_inbound(event: dict[str, Any]) -> InboundMessage | None:
    """Slack event -> InboundMessage, or None to ignore (bots, edits/joins, empty text after removing mentions)."""
    if event.get("bot_id") or event.get("subtype"):
        return None
    text = _MENTION.sub("", event.get("text", "")).strip()
    if not text:
        return None
    is_dm = event.get("channel_type") == "im"
    # Channel messages: thread under the triggering message. DMs: flat conversation.
    thread = None if is_dm else (event.get("thread_ts") or event["ts"])
    return InboundMessage(Address("slack", event["channel"], thread), event["user"], text, raw=event, is_group=not is_dm)


@channel_registry.register("slack")
class SlackChannel(Channel):
    max_len = 3500

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        if not self.extra.get("bot_token") or not self.extra.get("app_token"):
            raise ValueError("slack channel requires 'bot_token' and 'app_token'")
        # AsyncApp opens an aiohttp session, which needs a running event loop. The App builds channels before
        # the loop exists (asyncio.run(app.run())), so create the Slack objects in start().
        self.app: AsyncApp | None = None
        self._socket: AsyncSocketModeHandler | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, handler: MessageHandler) -> None:
        self.app = AsyncApp(token=self.extra["bot_token"])
        self._socket = AsyncSocketModeHandler(self.app, self.extra["app_token"])
        async def process(event: dict[str, Any]) -> None:
            if (msg := slack_inbound(event)) is not None:
                await handler(msg)

        @self.app.event("message")
        async def on_dm(event: dict[str, Any]) -> None:
            if event.get("channel_type") == "im":
                await process(event)

        @self.app.event("app_mention")
        async def on_mention(event: dict[str, Any]) -> None:
            await process(event)

        self._task = asyncio.create_task(self._socket.start_async(), name="slack")

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._socket:
                await self._socket.close_async()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def send(self, msg: OutboundMessage) -> None:
        assert self.app is not None, "channel not started"
        await self.app.client.chat_postMessage(channel=msg.address.conversation_id, text=msg.text,
                                               thread_ts=msg.address.thread_id)
