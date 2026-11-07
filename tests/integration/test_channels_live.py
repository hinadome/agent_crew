"""Live connectivity checks for each chat platform, using YOUR bot credentials (never your personal accounts).

These verify the adapters can authenticate and connect to the real service. They do NOT replace the manual
checklist in specs/17-testing-and-ci.md (a human must DM the bot to verify inbound routing, mentions and replies).

  Telegram   KIRO_IT_TELEGRAM_TOKEN               [+ KIRO_IT_TELEGRAM_CHAT to also send one test message]
  Discord    KIRO_IT_DISCORD_TOKEN                [+ KIRO_IT_DISCORD_CHANNEL (channel id) to also send]
  Slack      KIRO_IT_SLACK_BOT_TOKEN + KIRO_IT_SLACK_APP_TOKEN   [+ KIRO_IT_SLACK_CHANNEL to also send]
  WhatsApp   KIRO_IT_WHATSAPP_TOKEN + KIRO_IT_WHATSAPP_PHONE_ID  (read-only check; never sends)
"""

import asyncio
import os

import httpx
import pytest

from kiro_crew.channels import channel_class
from kiro_crew.core.config import ChannelConfig
from kiro_crew.core.types import Address, OutboundMessage

from .conftest import need_env


async def _noop(msg):
    return None


async def test_telegram_authenticates_and_can_send():
    env = need_env("KIRO_IT_TELEGRAM_TOKEN")
    ch = channel_class("telegram")("telegram", ChannelConfig(enabled=True, token=env["KIRO_IT_TELEGRAM_TOKEN"]))
    await ch.start(_noop)  # calls getMe, then begins long-polling
    try:
        assert ch._me.get("username"), "getMe returned no bot username"
        if os.environ.get("KIRO_IT_TELEGRAM_CHAT"):
            await ch.send(OutboundMessage(Address("telegram", os.environ["KIRO_IT_TELEGRAM_CHAT"]),
                                          "kiro-crew integration test: telegram send OK"))
    finally:
        await ch.stop()


async def test_discord_logs_in_and_becomes_ready():
    pytest.importorskip("discord")
    env = need_env("KIRO_IT_DISCORD_TOKEN")
    ch = channel_class("discord")("discord", ChannelConfig(enabled=True, token=env["KIRO_IT_DISCORD_TOKEN"]))
    await ch.start(_noop)
    try:
        for _ in range(60):
            if ch.client.is_ready():
                break
            await asyncio.sleep(0.5)
        assert ch.client.is_ready() and ch.client.user is not None
        if os.environ.get("KIRO_IT_DISCORD_CHANNEL"):
            await ch.send(OutboundMessage(Address("discord", os.environ["KIRO_IT_DISCORD_CHANNEL"]),
                                          "kiro-crew integration test: discord send OK"))
    finally:
        await ch.stop()


async def test_slack_socket_mode_connects_and_authenticates():
    pytest.importorskip("slack_bolt")
    env = need_env("KIRO_IT_SLACK_BOT_TOKEN", "KIRO_IT_SLACK_APP_TOKEN")
    ch = channel_class("slack")("slack", ChannelConfig(
        enabled=True, bot_token=env["KIRO_IT_SLACK_BOT_TOKEN"], app_token=env["KIRO_IT_SLACK_APP_TOKEN"]))
    await ch.start(_noop)
    try:
        await asyncio.sleep(3)  # let the socket-mode session establish
        auth = await ch.app.client.auth_test()
        assert auth["ok"] is True
        if os.environ.get("KIRO_IT_SLACK_CHANNEL"):
            await ch.send(OutboundMessage(Address("slack", os.environ["KIRO_IT_SLACK_CHANNEL"]),
                                          "kiro-crew integration test: slack send OK"))
    finally:
        await ch.stop()


async def test_whatsapp_token_and_phone_number_are_valid_readonly():
    env = need_env("KIRO_IT_WHATSAPP_TOKEN", "KIRO_IT_WHATSAPP_PHONE_ID")
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(f"https://graph.facebook.com/v21.0/{env['KIRO_IT_WHATSAPP_PHONE_ID']}",
                           params={"fields": "display_phone_number,verified_name"},
                           headers={"Authorization": f"Bearer {env['KIRO_IT_WHATSAPP_TOKEN']}"})
    assert r.status_code == 200 and "display_phone_number" in r.json()
