"""SafeFetcher / SSRF-protection unit tests.

Covers: URL validation, HTTPS-by-default, SSRF IP blocking (private, loopback,
link-local, reserved ranges), the loopback-only exception (offline simulation),
redirect validation, content-type and size limits, timeouts, canonicalization,
and fingerprinting. Uses a local server for the positive cases and the real
fetcher for the blocked cases (no network egress needed for blocks).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from factory.errors import UnsafeURLError
from factory.security.fetch import SafeFetcher, check_url_safety, is_blocked_ip
from factory.security.urls import canonicalize_url, registered_domain, url_fingerprint


class _TestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "https://10.0.0.1:9999/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/pdf":
            body = b"%PDF-1.4"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        if self.path == "/big":
            body = b"x" * 100_000
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
            return
        if self.path == "/slow":
            import time

            time.sleep(2.0)
        if self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"hello world"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


@pytest.fixture(scope="module")
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


class TestBlockedIPs:
    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "192.168.1.1",
            "127.0.0.1",
            "127.0.0.2",
            "169.254.169.254",  # cloud metadata
            "0.0.0.0",  # noqa: S104 - test data string, not a bind address
            "100.64.0.1",  # CGNAT shared range
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
        ],
    )
    def test_blocked_by_default(self, ip: str) -> None:
        assert is_blocked_ip(ip) is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_public_ips_allowed(self, ip: str) -> None:
        assert is_blocked_ip(ip) is False

    def test_unparseable_ip_blocked(self) -> None:
        assert is_blocked_ip("not-an-ip") is True


class TestURLSafety:
    def test_http_non_loopback_blocked_by_default(self) -> None:
        with pytest.raises(UnsafeURLError):
            check_url_safety("http://example.com/page")

    def test_http_loopback_blocked_by_default(self) -> None:
        with pytest.raises(UnsafeURLError):
            check_url_safety("http://127.0.0.1:8080/page")

    def test_http_loopback_allowed_with_flag(self) -> None:
        url = check_url_safety("http://127.0.0.1:8080/page", allow_loopback=True)
        assert url.startswith("http://127.0.0.1")

    def test_http_localhost_allowed_with_flag(self) -> None:
        check_url_safety("http://localhost:8080/page", allow_loopback=True)

    def test_private_ip_blocked_even_with_loopback_flag(self) -> None:
        with pytest.raises(UnsafeURLError):
            check_url_safety("https://10.0.0.1/x", allow_loopback=True)
        with pytest.raises(UnsafeURLError):
            check_url_safety("http://10.0.0.1/x", allow_loopback=True)
        with pytest.raises(UnsafeURLError):
            check_url_safety("https://192.168.1.1/x", allow_loopback=True)

    def test_embedded_credentials_rejected(self) -> None:
        with pytest.raises(UnsafeURLError):
            check_url_safety("https://user:secret@example.com/x")

    def test_bad_scheme_rejected(self) -> None:
        with pytest.raises(UnsafeURLError):
            check_url_safety("file:///etc/passwd")

    def test_unresolvable_host_rejected(self) -> None:
        with pytest.raises(UnsafeURLError):
            check_url_safety("https://nonexistent.invalid.domain.example/x")


class TestCanonicalization:
    def test_canonicalize(self) -> None:
        assert (
            canonicalize_url("https://Example.COM:443/a/?utm_source=x&b=2&a=1#frag")
            == "https://example.com/a?a=1&b=2"
        )

    def test_canonicalize_strips_trailing_slash(self) -> None:
        assert canonicalize_url("https://example.com/a/") == "https://example.com/a"

    def test_fingerprint_stable(self) -> None:
        assert url_fingerprint("https://Example.com/a?b=1") == url_fingerprint(
            "https://example.com/a?b=1&utm_source=x"
        )

    def test_registered_domain(self) -> None:
        assert registered_domain("en.wikipedia.org") == "wikipedia.org"
        assert registered_domain("a.b.example.com") == "example.com"


class TestFetcherAgainstLocalServer:
    def test_fetch_ok(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=5.0, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/target")
        assert doc.ok is True
        assert doc.content == "hello world"
        assert doc.content_type == "text/plain"
        assert doc.content_fingerprint

    def test_fetch_follows_safe_redirect(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=5.0, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/redirect")
        assert doc.ok is True
        assert doc.redirects == [f"{local_server}/target"]
        assert doc.final_url == f"{local_server}/target"

    def test_redirect_to_private_blocked(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=5.0, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/redirect-private")
        assert doc.ok is False
        assert "blocked address" in (doc.error or "")

    def test_disallowed_content_type(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=5.0, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/pdf")
        assert doc.ok is False
        assert "content type" in (doc.error or "")

    def test_oversized_response(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=5.0, max_bytes=1024, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/big")
        assert doc.ok is False
        assert "maximum size" in (doc.error or "")

    def test_timeout(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=0.5, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/slow")
        assert doc.ok is False
        assert "timed out" in (doc.error or "")

    def test_http_404_marked_failed(self, local_server: str) -> None:
        fetcher = SafeFetcher(timeout_seconds=5.0, allow_loopback=True)
        doc = fetcher.fetch(f"{local_server}/missing")
        assert doc.ok is False
        assert "404" in (doc.error or "")

    def test_unsafe_url_returns_failure_not_exception(self) -> None:
        fetcher = SafeFetcher()
        doc = fetcher.fetch("http://127.0.0.1/x")
        assert doc.ok is False
        assert "HTTPS" in (doc.error or "")

    def test_connection_refused_marked_failed(self) -> None:
        fetcher = SafeFetcher(timeout_seconds=2.0, allow_loopback=True)
        doc = fetcher.fetch("http://127.0.0.1:1/x")
        assert doc.ok is False
        assert doc.error
