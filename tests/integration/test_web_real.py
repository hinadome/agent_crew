"""web_fetch against the REAL network: DNS, TLS, redirects and sockets (no mock transports)."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from kiro_crew.tools import web
from kiro_crew.tools.web import FetchError, fetch_text

pytestmark = pytest.mark.usefixtures("network")


async def test_fetches_a_real_https_page_through_the_pinned_transport():
    # Real DNS -> vetted public IP -> TCP to the IP -> TLS with SNI = example.com -> real response.
    text = await fetch_text("https://example.com/")
    assert "Example Domain" in text


async def test_plain_http_works_too():
    assert "Example Domain" in await fetch_text("http://example.com/")


@pytest.mark.parametrize("url", [
    "https://expired.badssl.com/",
    "https://wrong.host.badssl.com/",
    "https://self-signed.badssl.com/",
])
async def test_tls_verification_is_not_weakened_by_ip_pinning(url):
    """The vital check: dialing an IP must still verify the certificate against the ORIGINAL hostname."""
    with pytest.raises(httpx.ConnectError) as e:
        await fetch_text(url)
    assert "CERTIFICATE_VERIFY_FAILED" in str(e.value) or "certificate" in str(e.value).lower()


async def test_real_localhost_and_dns_to_loopback_are_blocked():
    with pytest.raises(FetchError):
        await fetch_text("http://localhost:1/")
    # A real public DNS name that deliberately resolves to 127.0.0.1 (classic SSRF/rebinding fixture).
    try:
        await asyncio.to_thread(__import__("socket").getaddrinfo, "localtest.me", None)
    except OSError:
        pytest.skip("localtest.me does not resolve from here")
    with pytest.raises(FetchError, match="non-public"):
        await fetch_text("http://localtest.me/")


class _EchoHost(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"host={self.headers.get('Host')}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


async def test_connection_really_goes_to_the_pinned_ip_not_a_second_dns_lookup(monkeypatch):
    """Real sockets: 'rebind.invalid' does not exist in DNS. The fetch only works if the vetted IP is dialed
    directly (a second resolution would fail), and the server must still see Host: rebind.invalid."""
    srv = HTTPServer(("127.0.0.1", 0), _EchoHost)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        async def fake_resolve(host):
            return ["127.0.0.1"] if host == "rebind.invalid" else await real_resolve(host)

        real_resolve = web._resolve
        monkeypatch.setattr(web, "_resolve", fake_resolve)
        monkeypatch.setattr(web, "_is_public", lambda ip: True)  # pretend the loopback test address is public
        out = await fetch_text(f"http://rebind.invalid:{port}/")
        assert out == f"host=rebind.invalid:{port}"
    finally:
        srv.shutdown()
