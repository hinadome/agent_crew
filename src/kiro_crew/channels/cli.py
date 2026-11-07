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
        self._sent: asyncio.Event | None = None  # set whenever the agent prints something
        self.address = Address("cli", "local")

    async def start(self, on_message: MessageHandler) -> None:
        async def loop() -> None:
            print("kiro-crew ready. Type a message, /help for commands, Ctrl-D to quit.")
            pending: set[asyncio.Task[Any]] = set()
            self._sent = sent = asyncio.Event()
            while True:
                try:
                    line = await asyncio.to_thread(input, "you> ")
                except EOFError:
                    # let in-flight turns finish (piped input) before shutting down
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    self.quit.set()  # tell the app to shut down
                    return
                if line.strip():
                    # Dispatch concurrently so /approve can be read while a turn waits
                    # for approval; the gateway lock still serialises normal turns.
                    t: asyncio.Task[Any] = asyncio.ensure_future(
                        on_message(InboundMessage(self.address, "local", line, "you"))
                    )
                    pending.add(t)
                    t.add_done_callback(pending.discard)
                    # Show the next prompt only once the turn is finished, or once the agent says
                    # something mid-turn (e.g. an approval request) so /approve can be typed.
                    sent.clear()
                    waiter = asyncio.ensure_future(sent.wait())
                    await asyncio.wait({t, waiter}, return_when=asyncio.FIRST_COMPLETED)
                    waiter.cancel()

        self._task = asyncio.create_task(loop(), name="cli-input")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def send(self, msg: OutboundMessage) -> None:
        print(f"\nagent> {msg.text}\n")
        if self._sent is not None:
            self._sent.set()
