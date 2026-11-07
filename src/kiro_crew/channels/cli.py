"""Local terminal channel -- the easiest way to try the crew with no accounts or tokens."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..core.types import Address, InboundMessage, OutboundMessage
from . import Channel, MessageHandler, channel_registry


@channel_registry.register("cli")
class CLIChannel(Channel):
    local = True
    max_len = 100_000

    def __init__(self, *a: Any, **k: Any) -> None:
        super().__init__(*a, **k)
        self._task: asyncio.Task[None] | None = None
        self.address = Address("cli", "local")

    async def start(self, on_message: MessageHandler) -> None:
        async def loop() -> None:
            print("kiro-crew ready. Type a message, /help for commands, Ctrl-D to quit.")
            while True:
                try:
                    line = await asyncio.to_thread(input, "you> ")
                except EOFError:
                    self.quit.set()  # tell the app to shut down
                    return
                if line.strip():
                    await on_message(InboundMessage(self.address, "local", line, "you"))

        self._task = asyncio.create_task(loop(), name="cli-input")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def send(self, msg: OutboundMessage) -> None:
        print(f"\nagent> {msg.text}\n")
