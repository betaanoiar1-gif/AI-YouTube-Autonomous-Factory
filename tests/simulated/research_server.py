"""A realistic local simulation of the research plane's external services.

SIMULATED / OFFLINE — no real credentials, no real network egress.

The server implements, over real HTTP:

* a MediaWiki-shaped search API (``/api.php`` with
  ``action=query&list=search``) returning documented-shaped results, with
  tolerated per-result extensions (``url``, ``sourceType``) — the same
  extension points the production WebSourceProvider supports;
* document pages (``/wiki/<title>``) serving deterministic HTML content.

It binds ``0.0.0.0`` so that distinct loopback addresses (``127.0.0.1``,
``127.0.0.2``, ``127.0.0.3``, ``127.0.0.4``) act as distinct source "domains"
for source-independence and syndication testing. The production fetcher is
constructed with ``allow_loopback=True`` ONLY here (the offline simulation);
production fetchers never permit it.

The dataset is deterministic and includes: agreeing sources (multi-source
support), a conflicting date (contradiction), an exact syndicated duplicate
(same content fingerprint, different domain), and an explicit-disagreement
passage. Scriptable behaviors cover malformed responses, timeouts, oversized
bodies, wrong content types, unsafe redirects, and API errors.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: The four simulated source "domains" (distinct loopback addresses).
HOST_REFERENCE = "127.0.0.1"
HOST_ACADEMIC = "127.0.0.2"
HOST_JOURNALISM = "127.0.0.3"
HOST_MIRROR = "127.0.0.4"

_PAGE_REFERENCE = (
    "The Aqua Aqueduct was completed in 1312. "
    "The Aqua Aqueduct carried 250,000 cubic meters of water per day. "
    "The Aqua Aqueduct was built by the Roman Empire."
)
_PAGE_ACADEMIC = (
    "The Aqua Aqueduct was completed in 1312. "
    "The Aqua Aqueduct served a population of 1,200,000 people."
)
_PAGE_JOURNALISM = (
    "The Aqua Aqueduct was completed in 1305, according to conflicting reports. "
    "However, other sources dispute this date."
)
# Exact duplicate of the reference text (syndicated copy on another domain).
_PAGE_MIRROR = _PAGE_REFERENCE

_PAGES: dict[str, tuple[str, str]] = {
    "Ancient Aqua Aqueducts": (HOST_REFERENCE, _PAGE_REFERENCE),
    "Ancient Aqua Aqueducts (study)": (HOST_ACADEMIC, _PAGE_ACADEMIC),
    "Ancient Aqua Aqueducts (news)": (HOST_JOURNALISM, _PAGE_JOURNALISM),
    "Ancient Aqua Aqueducts (mirror)": (HOST_MIRROR, _PAGE_MIRROR),
}

_SOURCE_TYPES = {
    "Ancient Aqua Aqueducts": "reference",
    "Ancient Aqua Aqueducts (study)": "academic",
    "Ancient Aqua Aqueducts (news)": "journalism",
    "Ancient Aqua Aqueducts (mirror)": "secondary",
}


@dataclass
class SimulatedResearchBehavior:
    """How the simulated service should respond to the next request(s)."""

    status: int = 200
    # Response modes: normal | invalid_json | missing_query | oversized |
    # wrong_type | redirect_private
    mode: str = "normal"
    delay_seconds: float = 0.0


@dataclass
class SimulatedResearchRequest:
    method: str
    path: str
    host: str
    params: dict[str, str]


@dataclass
class SimulatedResearchState:
    behaviors: list[SimulatedResearchBehavior] = field(default_factory=list)
    requests: list[SimulatedResearchRequest] = field(default_factory=list)
    port: int = 0

    def queue(self, *behaviors: SimulatedResearchBehavior) -> None:
        self.behaviors.extend(behaviors)

    def next_behavior(self) -> SimulatedResearchBehavior:
        if self.behaviors:
            return self.behaviors.pop(0)
        return SimulatedResearchBehavior()

    def count(self, *, path_prefix: str | None = None) -> int:
        return sum(
            1 for r in self.requests if path_prefix is None or r.path.startswith(path_prefix)
        )


class _SimulatedResearchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: SimulatedResearchState) -> None:
        # Bind all interfaces so the distinct loopback "domains" are reachable.
        # Bind all interfaces so the distinct loopback "domains" are
        # reachable (offline simulation only).
        super().__init__(("0.0.0.0", 0), _SimulatedResearchHandler)  # noqa: S104
        self.sim_state = state


class _SimulatedResearchHandler(BaseHTTPRequestHandler):
    server: _SimulatedResearchHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default stderr logging."""

    @property
    def state(self) -> SimulatedResearchState:
        return self.server.sim_state

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionError):
            # The client may already have timed out and closed the socket.
            self.wfile.write(body)

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        params = {k: v[0] for k, v in parse_qs(split.query).items()}
        host = (self.headers.get("Host") or "").split(":")[0]
        self.state.requests.append(SimulatedResearchRequest("GET", split.path, host, params))
        behavior = self.state.next_behavior()
        if behavior.delay_seconds > 0:
            time.sleep(behavior.delay_seconds)

        if split.path == "/api.php":
            self._handle_search(params, behavior)
            return
        if split.path.startswith("/wiki/"):
            self._handle_page(split.path, behavior)
            return
        self._send(404, b"not found", "text/plain")

    def _handle_search(self, params: dict[str, str], behavior: SimulatedResearchBehavior) -> None:
        if behavior.status != 200:
            self._send(
                behavior.status,
                json.dumps({"error": {"code": behavior.status, "message": "simulated"}}).encode(),
                "application/json",
                extra_headers={"Retry-After": "1"} if behavior.status == 429 else None,
            )
            return
        if behavior.mode == "invalid_json":
            self._send(200, b"not json {{{", "application/json")
            return
        if behavior.mode == "missing_query":
            self._send(200, json.dumps({"batchcomplete": ""}).encode(), "application/json")
            return

        query = (params.get("srsearch") or "").lower()
        limit = int(params.get("srlimit", "10"))
        port = self.state.port
        # Match when any significant query term (len >= 4, not a stopword)
        # appears in the page title — a simple, deterministic relevance model.
        stopwords = {
            "the",
            "a",
            "an",
            "of",
            "in",
            "and",
            "to",
            "for",
            "with",
            "about",
            "what",
            "should",
            "viewers",
            "know",
            "is",
            "are",
            "was",
            "were",
            "how",
            "why",
            "when",
            "where",
            "who",
        }
        terms = [t for t in query.split() if len(t) >= 4 and t not in stopwords]
        results: list[dict[str, Any]] = []
        for title, (page_host, _text) in _PAGES.items():
            if terms and not any(term in title.lower() for term in terms):
                continue
            results.append(
                {
                    "title": title,
                    "snippet": f"Reference page about {title}.",
                    "pageid": abs(hash(title)) % 100000,
                    "url": f"http://{page_host}:{port}/wiki/{title.replace(' ', '_')}",
                    "sourceType": _SOURCE_TYPES[title],
                }
            )
            if len(results) >= limit:
                break
        payload = {
            "batchcomplete": "",
            "query": {"searchinfo": {"totalhits": len(results)}, "search": results},
        }
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _handle_page(self, path: str, behavior: SimulatedResearchBehavior) -> None:
        title = path[len("/wiki/") :].replace("_", " ")
        page = _PAGES.get(title)
        if behavior.status != 200:
            self._send(behavior.status, b"simulated error", "text/plain")
            return
        if behavior.mode == "oversized":
            body = b"<html><body><p>" + b"x" * 3_000_000 + b"</p></body></html>"
            self._send(200, body, "text/html")
            return
        if behavior.mode == "wrong_type":
            self._send(200, b"%PDF-1.4 simulated", "application/pdf")
            return
        if behavior.mode == "redirect_private":
            # HTTPS to a private address: the SSRF IP check must block it.
            self._send(
                302,
                b"",
                "text/html",
                extra_headers={"Location": "https://10.0.0.1:9999/private"},
            )
            return
        if page is None:
            self._send(404, b"no such page", "text/plain")
            return
        _host, text = page
        body = (
            f"<html><head><title>Simulated source</title></head><body><p>{text}</p></body></html>"
        ).encode()
        self._send(200, body, "text/html")


class SimulatedResearchServer:
    """Context-managed local simulation of the research plane's services."""

    def __init__(self) -> None:
        self.state = SimulatedResearchState()
        self._httpd = _SimulatedResearchHTTPServer(self.state)
        self.state.port = int(self._httpd.server_address[1])
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.state.port

    @property
    def search_api_url(self) -> str:
        """The simulated search API URL (MediaWiki-shaped)."""
        return f"http://{HOST_REFERENCE}:{self.port}/api.php"

    def page_url(self, host: str, title: str) -> str:
        return f"http://{host}:{self.port}/wiki/{title.replace(' ', '_')}"

    def start(self) -> SimulatedResearchServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> SimulatedResearchServer:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
