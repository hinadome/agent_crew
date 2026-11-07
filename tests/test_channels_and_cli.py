import asyncio
import hashlib
import hmac
import json
import socket

import httpx
import pytest

from kiro_crew.channels import channel_class, chunk_text
from kiro_crew.cli import main
from kiro_crew.core.config import ChannelConfig
from kiro_crew.core.types import Address


# ---- chunking / addresses ------------------------------------------------------------------------
@pytest.mark.parametrize("limit", [10, 50, 1900])
def test_chunk_text_respects_limit_and_loses_nothing(limit):
    text = "alpha beta gamma\n\n" * 200 + "x" * 3000
    parts = chunk_text(text, limit)
    assert all(0 < len(p) <= limit for p in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == text.replace(" ", "").replace("\n", "")


def test_chunk_text_edge_cases():
    assert chunk_text("", 100) == ["(empty response)"]
    assert chunk_text("short", 10) == ["short"]


def test_address_key_is_safe_and_distinct():
    a, b = Address("slack", "C1", "t1"), Address("slack", "C1", "t2")
    assert a.key != b.key and Address("slack", "C1").key != a.key
    assert "/" not in Address("x", "../../etc/passwd").key


# ---- adapters construct (optional dependencies are skipped if missing) -----------------------------
@pytest.mark.parametrize("name, extra, module", [
    ("discord", {"token": "x"}, "discord"),
    ("slack", {"bot_token": "xoxb-x", "app_token": "xapp-x"}, "slack_bolt"),
    ("telegram", {"token": "123:abc"}, None),
    ("whatsapp", dict(access_token="a", phone_number_id="1", verify_token="v", app_secret="s"), "aiohttp"),
])
def test_adapters_construct(name, extra, module):
    if module:
        pytest.importorskip(module)
    ch = channel_class(name)(name, ChannelConfig(enabled=True, **extra))
    assert ch.name == name and ch.max_len > 0


@pytest.mark.parametrize("name", ["discord", "slack", "telegram", "whatsapp"])
def test_adapters_require_credentials(name):
    pytest.importorskip({"discord": "discord", "slack": "slack_bolt", "whatsapp": "aiohttp"}.get(name, "httpx"))
    with pytest.raises(ValueError):
        channel_class(name)(name, ChannelConfig(enabled=True))


def test_unknown_channel_lists_available():
    with pytest.raises(KeyError, match="Available"):
        channel_class("carrier-pigeon")


# ---- WhatsApp webhook against a real local server -------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_whatsapp_webhook_security_and_dedupe():
    pytest.importorskip("aiohttp")
    port = _free_port()
    ch = channel_class("whatsapp")("whatsapp", ChannelConfig(
        enabled=True, access_token="a", phone_number_id="1", verify_token="vt", app_secret="sec",
        host="127.0.0.1", port=port))
    got = []

    async def handler(m):
        got.append(m)

    await ch.start(handler)
    base = f"http://127.0.0.1:{port}/whatsapp"
    try:
        async with httpx.AsyncClient() as http:
            ok = await http.get(base, params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
            assert ok.text == "42"
            bad = await http.get(base, params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"})
            assert bad.status_code == 403
            body = json.dumps({"entry": [{"changes": [{"value": {
                "contacts": [{"wa_id": "1555", "profile": {"name": "Z"}}],
                "messages": [{"id": "m1", "from": "1555", "type": "text", "text": {"body": "hello"}},
                             {"id": "m2", "from": "1555", "type": "image"}]}}]}]}).encode()
            sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
            assert (await http.post(base, content=body, headers={"X-Hub-Signature-256": "sha256=bad"})).status_code == 403
            assert (await http.post(base, content=body)).status_code == 403  # missing signature
            assert (await http.post(base, content=body, headers={"X-Hub-Signature-256": sig})).status_code == 200
            assert (await http.post(base, content=body, headers={"X-Hub-Signature-256": sig})).status_code == 200
        await asyncio.sleep(0.2)
        assert len(got) == 1  # replay de-duplicated, non-text ignored
        assert (got[0].text, got[0].user_id, got[0].user_name) == ("hello", "1555", "Z")
        assert got[0].address == Address("whatsapp", "1555")
    finally:
        await ch.stop()


# ---- CLI -------------------------------------------------------------------------------------------
@pytest.fixture
def cli_cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    p = tmp_path / "kiro-crew.yaml"
    p.write_text(p.read_text().replace("~/.kiro-crew", str(tmp_path / "ws")))
    return str(p)


def test_init_refuses_to_overwrite(cli_cfg):
    with pytest.raises(SystemExit):
        main(["init"])


def test_check_lists_plugins(cli_cfg, capsys):
    main(["-c", cli_cfg, "check"])
    out = capsys.readouterr().out
    assert "config OK" in out and "hybrid" in out and "sandbox" in out and "schedule" in out


def test_pairing_cli(cli_cfg, capsys):
    from kiro_crew.app import build_context
    from kiro_crew.core.config import load_config

    ctx = build_context(load_config(cli_cfg))
    code, _ = ctx.pairing.request("discord", "42", "Zed")
    ctx.storage.close()
    main(["-c", cli_cfg, "pairing", "list"])
    assert f"pending   discord:42 (Zed)  code {code}" in capsys.readouterr().out
    main(["-c", cli_cfg, "pairing", "approve", code])
    assert "approved discord:42" in capsys.readouterr().out
    main(["-c", cli_cfg, "pairing", "approve", "BADCODE"])
    assert "unknown or expired" in capsys.readouterr().out
    main(["-c", cli_cfg, "pairing", "revoke", "discord", "42"])
    assert "revoked" in capsys.readouterr().out


def test_memory_cli(cli_cfg, capsys):
    from kiro_crew.app import build_context
    from kiro_crew.core.config import load_config
    from kiro_crew.memory.factory import make_store

    ctx = build_context(load_config(cli_cfg))
    asyncio.run(make_store(ctx, "shared").add("Likes espresso"))
    ctx.db.close()
    main(["-c", cli_cfg, "memory", "list"])
    out = capsys.readouterr().out
    assert "1 memories" in out and "Likes espresso" in out
    main(["-c", cli_cfg, "memory", "search", "espresso"])
    assert "Likes espresso" in capsys.readouterr().out
    main(["-c", cli_cfg, "memory", "clear"])
    assert "refusing" in capsys.readouterr().out
    main(["-c", cli_cfg, "memory", "clear", "--yes"])
    assert "deleted 1" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="hybrid"):
        main(["-c", cli_cfg, "memory", "reindex"])


def test_schedule_and_prune_cli(cli_cfg, capsys):
    main(["-c", cli_cfg, "schedule", "list"])
    main(["-c", cli_cfg, "schedule", "cancel", "nope"])
    assert "not found" in capsys.readouterr().out
    main(["-c", cli_cfg, "prune"])
    assert "sessions" in capsys.readouterr().out


async def test_cli_channel_reads_input_while_a_turn_is_pending(monkeypatch):
    """Regression: /approve typed during a pending turn must be read, not queued behind it."""
    import asyncio

    from kiro_crew.channels.cli import CLIChannel

    lines = iter(["slow turn", "/approve"])

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    release = asyncio.Event()
    seen: list[str] = []

    async def handler(msg):
        seen.append(msg.text)
        if msg.text == "slow turn":
            await asyncio.wait_for(release.wait(), 5)  # blocks until /approve arrives
        else:
            release.set()

    ch = CLIChannel.__new__(CLIChannel)
    ch._task = None
    ch.quit = asyncio.Event()
    from kiro_crew.core.types import Address

    ch.address = Address("cli", "local")
    await ch.start(handler)
    await asyncio.wait_for(ch.quit.wait(), 5)
    assert seen == ["slow turn", "/approve"]
    await ch.stop()
