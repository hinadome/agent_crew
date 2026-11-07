"""Logs and the dashboard must be safe to enable: content hidden by default, secrets always masked."""

import io
import logging
import socket

import httpx
import pytest

from kiro_crew.app import App
from kiro_crew.cli import main
from kiro_crew.core import logging as L
from kiro_crew.core.config import Config

from .conftest import make_config

WEB_TOKEN = "WEBTOKEN-CANARY-98765"
CHAT = "my password is hunter2-CANARY-MSG"
TOOL_ARG = "TOOLARG-CANARY-555"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- secret scrubbing ---------------------------------------------------------------------------
def _fake(*parts: str) -> str:
    """Join fragments at test time.

    These are NOT credentials; they only have the *shape* of one so we can prove the scrubber recognises each
    provider's format. GitHub push protection matches on shape (not intent), so the repository must never contain a
    contiguous string that looks like a token. Each fragment alone is inert; ``test_repository_contains_no_secret_shaped
    _strings`` guards this.
    """
    return "".join(parts)


TOKENS = {
    "openai": _fake("s", "k-", "abcdefghijklmnopqrstuvwxyz", "123456"),
    "anthropic": _fake("s", "k-ant-", "api03-", "abcdefghijklmnopqrstuvwx"),
    "aws_access_key": _fake("AK", "IA", "IOSFODNN7", "EXAMPLE"),
    "slack_bot": _fake("xo", "xb-", "1234567890-", "abcdefghijkl"),
    "slack_app": _fake("xa", "pp-1-", "A0123456789-", "abcdefghij"),
    "telegram": _fake("123456789", ":A", "AH_", "abcdefghijklmnopqrstuvwxyz0123456"),
    "github": _fake("g", "hp_", "abcdefghijklmnopqrstuvwxyz", "0123456789"),
    "jwt": _fake("e", "yJhbGciOiJIUzI1NiJ9.", "e", "yJzdWIiOiIxMjM0NTY3ODkwIn0.", "abcdefghijklmnop"),
    "discord": _fake("MTIzNDU2Nzg5MDEyMzQ1Njc4", ".GabcDE.", "abcdefghijklmnopqrstuvwxyz0"),
}
OPENAI_LIKE = TOKENS["openai"]


@pytest.mark.parametrize("secret", list(TOKENS.values()), ids=list(TOKENS))
def test_token_shapes_are_masked(secret):
    out = L.scrub(f"calling api with {secret} now")
    assert secret not in out and "REDACTED" in out


@pytest.mark.parametrize("line, leaked", [
    ("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    ("GET /api/logs?token=supersecret99&limit=5", "supersecret99"),
    ('{"api_key": "hunter2hunter2"}', "hunter2hunter2"),
    ("password=Tr0ub4dor&3", "Tr0ub4dor"),
    ("app_secret: s3cr3tvalue", "s3cr3tvalue"),
])
def test_credential_key_value_forms_are_masked(line, leaked):
    assert leaked not in L.scrub(line)


def test_ordinary_text_untouched():
    text = "Meeting at 10:30 about the token economy and password policy docs"
    assert L.scrub(text) == text  # words alone are not secrets


def test_exact_config_secrets_masked_even_without_a_known_shape(tmp_path):
    cfg = make_config(tmp_path, channels={"fake": {"enabled": True, "allow_from": ["u1"], "bot_token": "plainsecret-xyz"}},
                      web_ui={"enabled": False, "token": "web-token-value"})
    found = L.collect_secrets(cfg)
    assert {"plainsecret-xyz", "web-token-value"} <= found
    assert "u1" not in found and "main" not in found  # allow-list ids and names are not secrets
    L.set_known_secrets(found)
    assert "plainsecret-xyz" not in L.scrub("value is plainsecret-xyz here")


def test_collect_secrets_reads_nested_model_client_keys():
    cfg = Config.model_validate({
        "workspace": "/tmp/x", "entry_agent": "main",
        "models": {"default": "a", "profiles": {"a": {"provider": "openai", "model_id": "m",
                                                      "client": {"api_key": "nested-api-key-1", "base_url": "http://x"}}}},
        "agents": {"main": {}}, "tool_options": {"web": {"api_key": "search-key-22"}}})
    assert {"nested-api-key-1", "search-key-22"} <= L.collect_secrets(cfg)


def test_redact_secrets_can_be_disabled():
    L.configure("INFO", redact_secrets=False)
    assert OPENAI_LIKE in L.scrub(OPENAI_LIKE)


def test_secret_in_formatted_log_line_and_extras_is_masked():
    r = L.configure("INFO")
    lg = logging.getLogger("kiro_crew.tools.web")
    lg.info(f"using key {OPENAI_LIKE}", extra={"header": "Bearer abcdefghijklmnop1234"})
    e = r.recent()[-1]
    assert OPENAI_LIKE[:9] not in e.message and "abcdefghijklmnop1234" not in str(e.fields)


def test_exception_tracebacks_are_scrubbed(capsys):
    L.configure("INFO")
    try:
        raise RuntimeError("upstream said token=leakedvalue123")
    except RuntimeError:
        logging.getLogger("kiro_crew.tasks").exception("task failed")
    err = capsys.readouterr().err
    assert "task failed" in err and "leakedvalue123" not in err


# ---- content hiding -----------------------------------------------------------------------------
def test_content_fields_hidden_by_default_and_visible_when_enabled():
    r = L.configure("DEBUG")
    lg = logging.getLogger("kiro_crew.gateway")
    L.log(lg, logging.DEBUG, "inbound", text="top secret chat", sender="u1", args={"x": "payload"})
    e = r.recent()[-1]
    assert "top secret chat" not in e.message and "payload" not in e.message
    assert "chars hidden" in e.message and e.fields["sender"] == "u1"  # metadata still visible

    r = L.configure("DEBUG", include_content=True)
    L.log(lg, logging.DEBUG, "inbound", text="top secret chat")
    assert "top secret chat" in r.recent()[-1].message


def test_third_party_debug_is_capped_while_content_hidden():
    L.configure("DEBUG", strands_level="DEBUG", areas={"httpx": "debug"})
    assert logging.getLogger("strands").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
    L.configure("DEBUG", strands_level="DEBUG", areas={"httpx": "debug"}, include_content=True)
    assert logging.getLogger("strands").level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.DEBUG


def test_cli_log_content_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    cfgp = tmp_path / "kiro-crew.yaml"
    cfgp.write_text(cfgp.read_text().replace("~/.kiro-crew", str(tmp_path / "ws")))
    main(["-c", str(cfgp), "--log-level", "INFO", "check"])
    assert L.policy().include_content is False
    main(["-c", str(cfgp), "--log-content", "check"])
    assert L.policy().include_content is True and L.policy().explicit


# ---- end to end: canaries through the real gateway, agent, tools, dashboard ---------------------
async def _canary_run(tmp_path, include_content):
    port = _free_port()
    app = App(make_config(
        tmp_path, web_ui={"enabled": True, "host": "127.0.0.1", "port": port, "token": WEB_TOKEN},
        logging={"level": "DEBUG", "include_content": include_content},
        agents={"main": {"tools": ["files"]}}))
    console = io.StringIO()
    sink = logging.StreamHandler(console)
    sink.addFilter(L._RedactFilter())
    logging.getLogger().addHandler(sink)
    await app.gateway.start()
    await app.web.start()
    try:
        ch = app.gateway.channels["fake"]
        await ch.say(CHAT)
        await ch.say('[[tool:write_file:{"path": "a.txt", "content": "' + TOOL_ARG + '"}]]')
        async with httpx.AsyncClient() as c:
            await c.get(f"http://127.0.0.1:{port}/api/state", params={"token": WEB_TOKEN})
            events = (await c.get(f"http://127.0.0.1:{port}/api/logs",
                                  params={"token": WEB_TOKEN, "limit": 5000})).json()
        feed = "\n".join(f"{e['message']} {e['fields']}" for e in events)
        return console.getvalue(), feed
    finally:
        await app.web.stop()
        await app.gateway.stop()
        app.ctx.db.close()
        logging.getLogger().removeHandler(sink)


async def test_default_logs_and_dashboard_leak_nothing(tmp_path):
    console, feed = await _canary_run(tmp_path, include_content=False)
    for where, blob in (("console", console), ("dashboard", feed)):
        assert WEB_TOKEN not in blob, f"web token leaked to {where}"
        assert "hunter2-CANARY-MSG" not in blob, f"chat text leaked to {where}"
        assert TOOL_ARG not in blob, f"tool argument leaked to {where}"
    assert "inbound message" in feed and "tool call" in feed  # still useful: the flow is visible


async def test_opt_in_shows_content_but_never_the_token(tmp_path):
    console, feed = await _canary_run(tmp_path, include_content=True)
    assert "hunter2-CANARY-MSG" in feed and TOOL_ARG in feed  # content appears when explicitly enabled
    assert WEB_TOKEN not in console and WEB_TOKEN not in feed  # ...but credentials are always masked


# ---- the repository must stay free of secret-shaped strings ------------------------------------------
# Approximations of the patterns GitHub push protection / secret scanning looks for. If this fails, a literal that
# looks like a credential was added to a tracked file: build it from fragments (see ``_fake``) or use an obviously
# non-token placeholder such as ``<your-key>``.
_SCANNER_SHAPES = {
    "OpenAI-style key": r"sk-(?:proj-)?[A-Za-z0-9]{20,}",
    "Anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "AWS access key id": r"\b(?:AKIA|ASIA|AROA|A3T[A-Z0-9])[A-Z0-9]{16}\b",
    "Slack token": r"xox[abprs]-[0-9A-Za-z-]{10,}|xapp-\d-[A-Za-z0-9-]{10,}",
    "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "Telegram bot token": r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}",
    "JWT": r"eyJ[A-Za-z0-9_=-]{10,}\.eyJ[A-Za-z0-9_=-]{10,}\.[A-Za-z0-9_=-]{10,}",
    "Discord bot token": r"\b[MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
    "private key block": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
}


def test_repository_contains_no_secret_shaped_strings():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist"}
    skip_files = {"uv.lock"}
    hits = []
    for path in root.rglob("*"):
        if (not path.is_file() or skip_dirs & set(path.relative_to(root).parts) or path.name in skip_files
                or path.suffix in {".pyc", ".png", ".jpg", ".whl", ".db"}):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in _SCANNER_SHAPES.items():
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{path.relative_to(root)}:{line} looks like a {label}")
    assert not hits, "secret-shaped strings found (GitHub push protection would reject these):\n" + "\n".join(hits)
