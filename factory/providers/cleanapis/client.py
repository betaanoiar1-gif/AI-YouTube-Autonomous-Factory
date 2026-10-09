"""Thin HTTP client for the CleanAPIs API (OpenAI-compatible).

Deliberately does NOT use an SDK: the provider boundary owns the translation
between our normalized models and the wire format, which keeps the CleanAPIs
integration explicit, testable (httpx.MockTransport), and free of SDK
version drift. See ADR-0007.

Verified wire facts (docs/cleanapis.md):

* ``POST {base}/chat/completions`` with ``{"model", "messages", ...}``.
* ``GET {base}/models`` returns ``{"object": "list", "data": [...]}``.
* Errors use the OpenAI-shaped envelope ``{"error": {"message", "type",
  "code", "param"?}}`` with statuses 401/402/403/404/422/429/502.
* 429 responses carry a ``Retry-After`` header (seconds).
* Rate-limit state is reported via ``X-RateLimit-*`` response headers.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, Field

from factory.config.provider_config import CleanAPISSettings
from factory.errors import (
    AuthenticationError,
    InsufficientFundsError,
    MalformedResponseError,
    ModelNotFoundError,
    ProviderConfigurationError,
    ProviderError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ProviderValidationError,
    RateLimitError,
    ScopeError,
)

PROVIDER_NAME = "cleanapis"


class ModelPricing(BaseModel):
    """Per-1K-token USD pricing reported by GET /models."""

    input_per_1k: float | None = None
    output_per_1k: float | None = None


class ModelInfo(BaseModel):
    """A model entry from GET /models."""

    id: str
    object: str | None = None
    owned_by: str | None = None
    name: str | None = None
    description: str | None = None
    type: str | None = None
    context_window: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    pricing: ModelPricing = Field(default_factory=ModelPricing)

    @property
    def supports_json_mode(self) -> bool:
        return "json_mode" in self.capabilities

    @property
    def estimated_cost_per_1k(self) -> float | None:
        """Combined input+output USD price per 1K tokens, when reported."""
        if self.pricing.input_per_1k is None or self.pricing.output_per_1k is None:
            return None
        return self.pricing.input_per_1k + self.pricing.output_per_1k


@dataclass
class RateLimitState:
    """Quota/rate-limit state parsed from X-RateLimit-* response headers."""

    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_at_epoch: int | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> RateLimitState:
        def _int(name: str) -> int | None:
            raw = headers.get(name)
            if raw is None:
                return None
            try:
                return int(raw)
            except ValueError:
                return None

        return cls(
            limit_requests=_int("x-ratelimit-limit"),
            remaining_requests=_int("x-ratelimit-remaining"),
            reset_at_epoch=_int("x-ratelimit-reset"),
            limit_tokens=_int("x-ratelimit-limit-tokens"),
            remaining_tokens=_int("x-ratelimit-remaining-tokens"),
        )


@dataclass
class ChatCompletionResult:
    """Raw normalized result of a chat completion call."""

    id: str | None
    model: str
    content: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    rate_limits: RateLimitState = field(default_factory=RateLimitState)
    raw: dict[str, Any] = field(default_factory=dict)


class CleanAPIsClient:
    """Minimal, explicit client for the CleanAPIs OpenAI-compatible API."""

    def __init__(
        self,
        settings: CleanAPISSettings,
        *,
        http_client: httpx.Client | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._owns_client = http_client is None
        api_key = settings.cleanapis_API_KEY
        if not api_key:
            raise ProviderConfigurationError(
                "CleanAPIs API key is not configured (set cleanapis_API_KEY)",
                provider=PROVIDER_NAME,
            )
        self._client = http_client or httpx.Client(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_seconds, connect=10.0),
        )
        # Auth is applied to injected clients too (e.g. test transports), so a
        # client built by the caller can never send an unauthenticated request.
        self._client.headers["Authorization"] = f"Bearer {api_key}"
        self._client.headers["Content-Type"] = "application/json"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CleanAPIsClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """GET /models — verifies authentication and endpoint reachability."""
        response = self._request("GET", "/models")
        try:
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, list):
                raise ValueError("'data' is not a list")
            return [ModelInfo.model_validate(item) for item in data]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            raise MalformedResponseError(
                f"Malformed /models response: {exc}",
                provider=PROVIDER_NAME,
            ) from exc

    def chat_completion(self, payload: dict[str, Any]) -> ChatCompletionResult:
        """POST /chat/completions with retry/backoff for transient errors."""
        response = self._request("POST", "/chat/completions", json_body=payload)
        return self._parse_completion(response)

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute a request with retries for 429/5xx; map all errors."""
        max_retries = self._settings.max_retries
        attempt = 0
        while True:
            try:
                response = self._client.request(method, path, json=json_body)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    f"CleanAPIs request timed out after {self._settings.timeout_seconds}s: "
                    f"{method} {path}",
                    provider=PROVIDER_NAME,
                ) from exc
            except httpx.ConnectError as exc:
                raise ProviderUnavailableError(
                    f"CleanAPIs endpoint is unreachable: {method} {path} ({exc})",
                    provider=PROVIDER_NAME,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderHTTPError(
                    f"CleanAPIs HTTP error: {exc}", provider=PROVIDER_NAME
                ) from exc

            if response.status_code < 400:
                return response

            error = self._map_error(response, method, path)
            retryable = isinstance(error, RateLimitError) or (
                isinstance(error, ProviderHTTPError)
                and error.status_code is not None
                and error.status_code >= 500
            )
            if retryable and attempt < max_retries:
                delay = self._retry_delay(error, attempt)
                self._sleep(delay)
                attempt += 1
                continue
            raise error

    def _retry_delay(self, error: ProviderError, attempt: int) -> float:
        """Exponential backoff with jitter; honors Retry-After when present."""
        if isinstance(error, RateLimitError) and error.retry_after_seconds is not None:
            retry_after: float = error.retry_after_seconds
            return max(retry_after, 0.0)
        base: float = 0.5 * (2**attempt)
        jitter: float = random.uniform(0, 0.25)  # noqa: S311 - jitter is not security-relevant
        return base + jitter

    def _map_error(self, response: httpx.Response, method: str, path: str) -> ProviderError:
        """Map an HTTP error response to a typed, sanitized ProviderError."""
        status = response.status_code
        message, code = self._parse_error_envelope(response)
        kwargs: dict[str, Any] = {
            "provider": PROVIDER_NAME,
            "status_code": status,
            "error_code": code,
        }
        if status == 401:
            return AuthenticationError(
                message or "Authentication failed: missing, malformed, or revoked API key", **kwargs
            )
        if status == 402:
            return InsufficientFundsError(
                message or "Insufficient funds: balance and plan allowance exhausted", **kwargs
            )
        if status == 403:
            return ScopeError(message or "The API key lacks the required scope", **kwargs)
        if status == 404:
            return ModelNotFoundError(message or "Model or endpoint not found", **kwargs)
        if status == 422:
            return ProviderValidationError(
                message or "Request body failed provider validation", **kwargs
            )
        if status == 429:
            retry_after = response.headers.get("retry-after")
            retry_seconds: float | None = None
            if retry_after is not None:
                try:
                    retry_seconds = float(retry_after)
                except ValueError:
                    retry_seconds = None
            return RateLimitError(
                message or "Rate limit exceeded",
                retry_after_seconds=retry_seconds,
                **kwargs,
            )
        if status == 502:
            return ProviderHTTPError(message or "Upstream model provider failed", **kwargs)
        return ProviderHTTPError(
            message or f"Unexpected HTTP {status} from CleanAPIs ({method} {path})", **kwargs
        )

    @staticmethod
    def _parse_error_envelope(
        response: httpx.Response,
    ) -> tuple[str | None, str | None]:
        """Extract (message, code) from the documented error envelope."""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return None, None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None, None
        message = error.get("message")
        code = error.get("code")
        return (
            message if isinstance(message, str) else None,
            code if isinstance(code, str) else None,
        )

    @staticmethod
    def _parse_completion(response: httpx.Response) -> ChatCompletionResult:
        """Parse and structurally validate a chat completion response."""
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponseError(
                f"Chat completion response is not valid JSON: {exc}",
                provider=PROVIDER_NAME,
            ) from exc
        try:
            choices = payload["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError("'choices' is missing or empty")
            choice = choices[0]
            message = choice["message"]
            content = message.get("content")
            if not isinstance(content, str):
                raise ValueError("'choices[0].message.content' is missing or not a string")
            usage = payload.get("usage") or {}
            return ChatCompletionResult(
                id=payload.get("id") if isinstance(payload.get("id"), str) else None,
                model=payload.get("model") if isinstance(payload.get("model"), str) else "",
                content=content,
                finish_reason=(
                    choice.get("finish_reason")
                    if isinstance(choice.get("finish_reason"), str)
                    else None
                ),
                prompt_tokens=_optional_int(usage.get("prompt_tokens")),
                completion_tokens=_optional_int(usage.get("completion_tokens")),
                total_tokens=_optional_int(usage.get("total_tokens")),
                rate_limits=RateLimitState.from_headers(response.headers),
                raw=payload,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedResponseError(
                f"Malformed chat completion response: {exc}",
                provider=PROVIDER_NAME,
            ) from exc


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
