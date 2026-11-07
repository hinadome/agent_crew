"""WhatsApp via the official Meta WhatsApp Cloud API (webhook). Needs a public HTTPS URL (e.g. a tunnel).

Config: ``access_token``, ``phone_number_id``, ``verify_token`` (you choose), ``app_secret`` (verifies
webhook signatures -- required), optional ``host`` (0.0.0.0), ``port`` (8080), ``path`` (/whatsapp).
Allow-list entries are the sender's phone number in international format without '+', e.g. "15551234567".

Note: free-form replies are only allowed within 24h of the user's last message (WhatsApp policy), so
long-running task results sent later may need an approved template -- not handled here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections import OrderedDict
from typing import Any

import httpx
from aiohttp import web

from ..core.types import Address, InboundMessage, OutboundMessage
from . import Channel, MessageHandler, channel_registry

logger = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com/v21.0"


@channel_registry.register("whatsapp")
class WhatsAppChannel(Channel):
    max_len = 3800  # hard cap is 4096

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        missing = [x for x in ("access_token", "phone_number_id", "verify_token", "app_secret")
                   if not self.extra.get(x)]
        if missing:
            raise ValueError(f"whatsapp channel requires {missing}")
        self._http = httpx.AsyncClient(timeout=30)
        self._runner: web.AppRunner | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()  # Meta retries deliveries; dedupe by message id
        self._pending: set[asyncio.Task[None]] = set()  # keep references so background turns are not GC'd

    @staticmethod
    async def _dispatch(handler: MessageHandler, msg: InboundMessage) -> None:
        await handler(msg)

    async def start(self, handler: MessageHandler) -> None:
        path = self.extra.get("path", "/whatsapp")

        async def verify(req: web.Request) -> web.Response:
            q = req.query
            if q.get("hub.mode") == "subscribe" and hmac.compare_digest(
                    q.get("hub.verify_token", ""), str(self.extra["verify_token"])):
                return web.Response(text=q.get("hub.challenge", ""))
            return web.Response(status=403)

        async def receive(req: web.Request) -> web.Response:
            body = await req.read()
            expected = "sha256=" + hmac.new(str(self.extra["app_secret"]).encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, req.headers.get("X-Hub-Signature-256", "")):
                return web.Response(status=403)
            payload = await req.json()
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    names = {c["wa_id"]: c.get("profile", {}).get("name")
                             for c in change["value"].get("contacts", [])}
                    for m in change["value"].get("messages", []):
                        if m.get("type") != "text" or m["id"] in self._seen:
                            continue
                        self._seen[m["id"]] = None
                        while len(self._seen) > 2000:
                            self._seen.popitem(last=False)
                        sender = m["from"]
                        # Ack fast (Meta retries slow webhooks); process in the background.
                        inbound = InboundMessage(Address("whatsapp", sender), sender, m["text"]["body"],
                                                 names.get(sender), raw=m)
                        self._pending.add(task := asyncio.create_task(self._dispatch(handler, inbound)))
                        task.add_done_callback(self._pending.discard)
            return web.Response(text="ok")

        app = web.Application()
        app.add_routes([web.get(path, verify), web.post(path, receive)])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.extra.get("host", "0.0.0.0"), int(self.extra.get("port", 8080)))
        await site.start()
        logger.info("WhatsApp webhook listening on :%s%s", self.extra.get("port", 8080), path)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        await self._http.aclose()

    async def send(self, msg: OutboundMessage) -> None:
        r = await self._http.post(
            f"{GRAPH}/{self.extra['phone_number_id']}/messages",
            headers={"Authorization": f"Bearer {self.extra['access_token']}"},
            json={"messaging_product": "whatsapp", "to": msg.address.conversation_id, "type": "text",
                  "text": {"body": msg.text}})
        r.raise_for_status()
