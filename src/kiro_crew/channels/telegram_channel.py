"""Telegram Bot API via httpx long-polling (no extra dependency). Config: ``token`` from @BotFather.

Private chats: always replies. Groups: only when @mentioned or when replying to the bot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

import httpx

from ..core.types import Address, InboundMessage, OutboundMessage
from . import Channel, MessageHandler, channel_registry

logger = logging.getLogger(__name__)


@channel_registry.register("telegram")
class TelegramChannel(Channel):
    max_len = 4000  # hard cap is 4096

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        token = self.extra.get("token")
        if not token:
            raise ValueError("telegram channel requires 'token'")
        self._http = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}", timeout=70)
        self._task: asyncio.Task[None] | None = None
        self._me: dict = {}

    async def _call(self, method: str, **params: Any) -> Any:
        r = await self._http.post(f"/{method}", json=params)
        r.raise_for_status()
        return r.json()["result"]

    async def start(self, handler: MessageHandler) -> None:
        self._me = await self._call("getMe")
        username = self._me["username"].lower()

        async def poll() -> None:
            offset = 0
            while True:
                try:
                    updates = await self._call("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
                except Exception as e:  # noqa: BLE001  (CancelledError is a BaseException and still propagates)
                    logger.warning("telegram poll error: %s", e)
                    await asyncio.sleep(5)
                    continue
                for u in updates:
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    text = (m.get("text") or "").strip()
                    if not text or m.get("from", {}).get("is_bot"):
                        continue
                    group = m["chat"]["type"] != "private"
                    if group:
                        replied_to_bot = (m.get("reply_to_message", {}).get("from", {}).get("username", "")
                                          .lower() == username)
                        if f"@{username}" not in text.lower() and not replied_to_bot:
                            continue
                        text = re.sub(r"[ \t]{2,}", " ", text.replace(f"@{self._me['username']}", "")).strip()
                    await handler(InboundMessage(Address("telegram", str(m["chat"]["id"])), str(m["from"]["id"]),
                                                 text, m["from"].get("first_name"), raw=m, is_group=group))

        self._task = asyncio.create_task(poll(), name="telegram")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._http.aclose()

    async def send(self, msg: OutboundMessage) -> None:
        await self._call("sendMessage", chat_id=int(msg.address.conversation_id), text=msg.text)

    async def typing(self, address: Address) -> None:
        await self._call("sendChatAction", chat_id=int(address.conversation_id), action="typing")
