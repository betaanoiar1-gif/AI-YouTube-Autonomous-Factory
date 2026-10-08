"""A realistic local simulation of the external CleanAPIs HTTP API.

SIMULATED / OFFLINE — no real credentials, no real network egress.

The server implements the *documented* CleanAPIs surface (verified from
https://cleanapis.com/docs) over real HTTP on ``127.0.0.1``:

* ``GET /v1/models`` — the documented models-list shape (id, context window,
  capabilities, pricing) so model parsing and cheapest-model selection run
  against realistic data;
* ``POST /v1/chat/completions`` — the documented request/response shapes,
  including ``usage`` token metadata and ``X-RateLimit-*`` headers;
* Bearer authentication (``Authorization: Bearer cc_...`` and ``x-api-key``)
  with the documented ``401`` error envelope for missing/invalid keys;
* the documented error envelope ``{"error": {"message", "type", "code"}}``
  for 4xx/5xx responses, and ``429`` with a ``Retry-After`` header;
* scriptable per-request behaviors: malformed JSON, missing/empty choices,
  non-string content, omitted usage, arbitrary HTTP status, delayed
  responses (to exercise client timeouts), and rate limiting.

The production ``CleanAPIsClient``/``CleanAPIsProvider`` talk to this server
exactly as they would talk to the real service — same URLs, same auth, same
parsing — which is what makes the tests realistic without credentials.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# A clearly-fake key. NOT a real credential — the simulation accepts exactly
# this value (and nothing else) and the tests never use a real key.
SIMULATED_API_KEY = "cc_simulated_offline_key_0000000001"  # fake value, offline only

SIMULATED_MODEL_CHEAP = "simulated-cheap"
SIMULATED_MODEL_PREMIUM = "simulated-premium"

_MODELS_PAYLOAD: dict[str, Any] = {
    "object": "list",
    "data": [
        {
            "id": SIMULATED_MODEL_PREMIUM,
            "object": "model",
            "created": 1787240928,
            "owned_by": "Clean APIs",
            "name": "Simulated Premium",
            "description": "Simulated premium model (offline test double).",
            "type": "chat",
            "context_window": 128000,
            "capabilities": ["reasoning", "vision", "tools", "streaming", "json_mode"],
            "pricing": {"input_per_1k": 0.01, "output_per_1k": 0.02},
        },
        {
            "id": SIMULATED_MODEL_CHEAP,
            "object": "model",
            "created": 1787240928,
            "owned_by": "Clean APIs",
            "name": "Simulated Cheap",
            "description": "Simulated cheap model (offline test double).",
            "type": "chat",
            "context_window": 32000,
            "capabilities": ["json_mode", "streaming"],
            "pricing": {"input_per_1k": 0.0001, "output_per_1k": 0.0002},
        },
    ],
}

_RATE_LIMIT_HEADERS: dict[str, str] = {
    "X-RateLimit-Limit": "300",
    "X-RateLimit-Remaining": "299",
    "X-RateLimit-Reset": "1787305980",
    "X-RateLimit-Limit-Tokens": "500000",
    "X-RateLimit-Remaining-Tokens": "499982",
}


@dataclass
class SimulatedBehavior:
    """How the simulated service should respond to the next request(s)."""

    status: int = 200
    # Response body modes for chat completions:
    #   normal | invalid_json | missing_choices | empty_choices |
    #   non_string_content | omit_usage
    mode: str = "normal"
    content: str = "Hello from the simulated CleanAPIs service."
    retry_after: float | None = None  # seconds, sent as Retry-After on 429
    delay_seconds: float = 0.0  # response delay (client timeout simulation)
    include_rate_limit_headers: bool = True
    include_usage: bool = True


@dataclass
class SimulatedRequest:
    """One request observed by the simulated service."""

    method: str
    path: str
    authorized: bool
    model: str | None
    body: dict[str, Any] | None
    behavior_mode: str


@dataclass
class SimulatedCleanAPISState:
    """Mutable state shared between the server and the tests."""

    valid_key: str = SIMULATED_API_KEY
    behaviors: list[SimulatedBehavior] = field(default_factory=list)
    requests: list[SimulatedRequest] = field(default_factory=list)
    chat_bodies: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, *behaviors: SimulatedBehavior) -> None:
        self.behaviors.extend(behaviors)

    def next_behavior(self) -> SimulatedBehavior:
        if self.behaviors:
            return self.behaviors.pop(0)
        return SimulatedBehavior()

    @property
    def request_count(self) -> int:
        return len(self.requests)

    @property
    def chat_request_count(self) -> int:
        return len(self.chat_bodies)


class _SimulatedCleanAPISHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying the shared simulation state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: SimulatedCleanAPISState) -> None:
        super().__init__(("127.0.0.1", 0), _SimulatedCleanAPISHandler)
        self.sim_state = state


class _SimulatedCleanAPISHandler(BaseHTTPRequestHandler):
    """Implements the documented CleanAPIs endpoints."""

    server: _SimulatedCleanAPISHTTPServer

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """Silence default stderr logging."""

    @property
    def state(self) -> SimulatedCleanAPISState:
        return self.server.sim_state

    def _authorized(self) -> bool:
        expected = f"Bearer {self.state.valid_key}"
        return (
            self.headers.get("Authorization") == expected
            or self.headers.get("x-api-key") == self.state.valid_key
        )

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any] | str,
        *,
        extra_headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8") if raw_body is None else raw_body
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionError):
            # The client may already have timed out and closed the socket.
            self.wfile.write(body)

    def _send_error(
        self,
        status: int,
        message: str,
        error_type: str,
        code: str | None = None,
        *,
        retry_after: float | None = None,
    ) -> None:
        error: dict[str, Any] = {"message": message, "type": error_type}
        if code:
            error["code"] = code
        headers = dict(_RATE_LIMIT_HEADERS)
        if retry_after is not None:
            headers["Retry-After"] = str(int(retry_after))
        self._send_json(status, {"error": error}, extra_headers=headers)

    # -- endpoints --------------------------------------------------------

    def do_GET(self) -> None:
        if not self._authorized():
            self.state.requests.append(
                SimulatedRequest("GET", self.path, False, None, None, "unauthorized")
            )
            self._send_error(
                401,
                "Missing or invalid API key.",
                "authentication_error",
                "invalid_api_key",
            )
            return
        if self.path == "/v1/models":
            self.state.requests.append(
                SimulatedRequest("GET", self.path, True, None, None, "models")
            )
            self._send_json(200, _MODELS_PAYLOAD, extra_headers=dict(_RATE_LIMIT_HEADERS))
            return
        self._send_error(404, f"Unknown path: {self.path}", "invalid_request_error", "not_found")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}

        if not self._authorized():
            self.state.requests.append(
                SimulatedRequest("POST", self.path, False, None, body, "unauthorized")
            )
            self._send_error(
                401,
                "Missing or invalid API key.",
                "authentication_error",
                "invalid_api_key",
            )
            return
        if self.path != "/v1/chat/completions":
            self._send_error(
                404, f"Unknown path: {self.path}", "invalid_request_error", "not_found"
            )
            return

        behavior = self.state.next_behavior()
        self.state.chat_bodies.append(body)
        self.state.requests.append(
            SimulatedRequest(
                "POST",
                self.path,
                True,
                body.get("model") if isinstance(body.get("model"), str) else None,
                body,
                behavior.mode if behavior.status == 200 else f"status_{behavior.status}",
            )
        )

        if behavior.delay_seconds > 0:
            time.sleep(behavior.delay_seconds)

        headers = dict(_RATE_LIMIT_HEADERS) if behavior.include_rate_limit_headers else {}
        if behavior.retry_after is not None:
            headers["Retry-After"] = str(int(behavior.retry_after))

        if behavior.status != 200:
            self._send_error(
                behavior.status,
                f"Simulated service error (HTTP {behavior.status}).",
                "provider_error" if behavior.status >= 500 else "invalid_request_error",
                f"simulated_{behavior.status}",
                retry_after=behavior.retry_after if behavior.status == 429 else None,
            )
            return

        if behavior.mode == "invalid_json":
            self._send_json(
                200,
                "",
                raw_body=b"this is not json {{{",
                extra_headers=headers,
            )
            return

        model = body.get("model") if isinstance(body.get("model"), str) else "unknown"
        completion: dict[str, Any] = {
            "id": "chatcmpl-simulated-0001",
            "object": "chat.completion",
            "created": 1787305982,
            "model": model,
        }
        if behavior.mode == "missing_choices":
            self._send_json(200, completion, extra_headers=headers)
            return
        if behavior.mode == "empty_choices":
            completion["choices"] = []
            self._send_json(200, completion, extra_headers=headers)
            return
        content: Any = None if behavior.mode == "non_string_content" else behavior.content
        completion["choices"] = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
        if behavior.include_usage:
            completion["usage"] = {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }
        self._send_json(200, completion, extra_headers=headers)


class SimulatedCleanAPISServer:
    """Context-managed local simulation of the CleanAPIs HTTP API.

    Usage::

        with SimulatedCleanAPISServer() as server:
            settings = CleanAPISSettings(
                cleanapis_API_KEY=SIMULATED_API_KEY,
                base_url=server.base_url,
                model=SIMULATED_MODEL_CHEAP,
            )
            provider = CleanAPIsProvider(settings)  # production code path
            ...
    """

    def __init__(self, *, valid_key: str = SIMULATED_API_KEY) -> None:
        self.state = SimulatedCleanAPISState(valid_key=valid_key)
        self._httpd = _SimulatedCleanAPISHTTPServer(self.state)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        """Base URL of the simulated service, ending in ``/v1`` like the real one."""
        address = self._httpd.server_address
        return f"http://{address[0]!s}:{int(address[1])}/v1"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> SimulatedCleanAPISServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> SimulatedCleanAPISServer:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()
