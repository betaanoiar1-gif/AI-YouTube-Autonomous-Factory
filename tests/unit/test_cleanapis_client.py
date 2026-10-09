"""CleanAPIs client tests — HTTP layer fully mocked (no network, no real key).

Covers the required cases: successful request, invalid key (401), HTTP
errors (402/403/404/422/5xx), timeout, rate limiting with Retry-After, and
malformed responses.
"""

from __future__ import annotations

import httpx
import pytest

from factory.errors import (
    AuthenticationError,
    InsufficientFundsError,
    MalformedResponseError,
    ModelNotFoundError,
    ProviderConfigurationError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderValidationError,
    RateLimitError,
    ScopeError,
)
from factory.providers.cleanapis.client import CleanAPIsClient, ModelInfo
from tests.conftest import FAKE_API_KEY, chat_completion_response, error_response


def test_client_requires_api_key(cleanapis_settings):
    from factory.config.provider_config import CleanAPISSettings

    settings = CleanAPISSettings(cleanapis_API_KEY=None)
    with pytest.raises(ProviderConfigurationError):
        CleanAPIsClient(settings)


def test_list_models_parses_documented_shape(mock_client_factory):
    client = mock_client_factory(
        lambda request: httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "claude-opus-4.8",
                        "object": "model",
                        "created": 1787240928,
                        "owned_by": "Clean APIs",
                        "name": "Example Model",
                        "description": "General-purpose chat model.",
                        "type": "chat",
                        "context_window": 128000,
                        "capabilities": ["reasoning", "vision", "tools", "streaming", "json_mode"],
                        "pricing": {"input_per_1k": 0.000115, "output_per_1k": 0.00023},
                    },
                    {
                        "id": "cheap-model",
                        "object": "model",
                        "created": 1787240928,
                        "owned_by": "Clean APIs",
                        "context_window": 32000,
                        "capabilities": ["json_mode"],
                        "pricing": {"input_per_1k": 0.00001, "output_per_1k": 0.00002},
                    },
                ],
            },
        )
    )
    models = client.list_models()
    assert len(models) == 2
    assert models[0].id == "claude-opus-4.8"
    assert models[0].context_window == 128000
    assert models[0].supports_json_mode is True
    assert models[0].estimated_cost_per_1k == pytest.approx(0.000345)
    assert models[1].estimated_cost_per_1k == pytest.approx(0.00003)


def test_list_models_malformed_raises(mock_client_factory):
    client = mock_client_factory(lambda request: httpx.Response(200, json={"unexpected": True}))
    with pytest.raises(MalformedResponseError):
        client.list_models()


def test_chat_completion_success(mock_client_factory):
    client = mock_client_factory(lambda request: chat_completion_response(content="Hi there"))
    result = client.chat_completion({"model": "test-model", "messages": []})
    assert result.content == "Hi there"
    assert result.model == "test-model"
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5
    assert result.total_tokens == 15
    assert result.id == "chatcmpl-test-1"
    assert result.rate_limits.limit_requests == 300
    assert result.rate_limits.remaining_tokens == 499985


def test_chat_completion_sends_bearer_auth(mock_client_factory):
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return chat_completion_response()

    client = mock_client_factory(handler)
    client.chat_completion({"model": "test-model", "messages": []})
    assert seen_headers[0]["authorization"] == f"Bearer {FAKE_API_KEY}"


def test_401_maps_to_authentication_error(mock_client_factory):
    client = mock_client_factory(
        lambda request: error_response(
            401, "Missing or invalid API key.", "authentication_error", "invalid_api_key"
        )
    )
    with pytest.raises(AuthenticationError) as exc_info:
        client.list_models()
    assert exc_info.value.status_code == 401
    assert exc_info.value.error_code == "invalid_api_key"


def test_402_maps_to_insufficient_funds(mock_client_factory):
    client = mock_client_factory(
        lambda request: error_response(402, "Out of balance.", "insufficient_funds")
    )
    with pytest.raises(InsufficientFundsError):
        client.list_models()


def test_403_maps_to_scope_error(mock_client_factory):
    client = mock_client_factory(
        lambda request: error_response(
            403, "Missing scope.", "invalid_request_error", "insufficient_scope"
        )
    )
    with pytest.raises(ScopeError):
        client.list_models()


def test_404_maps_to_model_not_found(mock_client_factory):
    client = mock_client_factory(
        lambda request: error_response(
            404, "Unknown model.", "invalid_request_error", "model_not_found"
        )
    )
    with pytest.raises(ModelNotFoundError):
        client.chat_completion({"model": "nope", "messages": []})


def test_422_maps_to_validation_error(mock_client_factory):
    client = mock_client_factory(
        lambda request: error_response(422, "Bad body.", "invalid_request_error")
    )
    with pytest.raises(ProviderValidationError):
        client.chat_completion({"model": "x", "messages": []})


def test_429_retried_with_retry_after_then_succeeds(mock_client_factory):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "Rate limit exceeded.",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                },
                headers={"Retry-After": "2"},
            )
        return chat_completion_response()

    client = mock_client_factory(handler)
    result = client.chat_completion({"model": "test-model", "messages": []})
    assert result.content == "Hello!"
    assert len(calls) == 2


def test_429_exhausted_raises_rate_limit_error(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "Rate limit exceeded.", "type": "rate_limit_error"}},
            headers={"Retry-After": "1"},
        )

    client = mock_client_factory(handler)
    with pytest.raises(RateLimitError) as exc_info:
        client.chat_completion({"model": "test-model", "messages": []})
    assert exc_info.value.retry_after_seconds == 1.0


def test_500_retried_then_raises_provider_http_error(mock_client_factory):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return error_response(502, "Upstream failed.", "provider_error")

    client = mock_client_factory(handler)
    with pytest.raises(ProviderHTTPError) as exc_info:
        client.chat_completion({"model": "test-model", "messages": []})
    assert exc_info.value.status_code == 502
    # 1 initial attempt + max_retries (2) retries.
    assert len(calls) == 3


def test_timeout_maps_to_provider_timeout(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = mock_client_factory(handler)
    with pytest.raises(ProviderTimeoutError):
        client.chat_completion({"model": "test-model", "messages": []})


def test_connect_error_maps_to_provider_unavailable(mock_client_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = mock_client_factory(handler)
    from factory.errors import ProviderUnavailableError

    with pytest.raises(ProviderUnavailableError):
        client.list_models()


def test_malformed_json_raises(mock_client_factory):
    client = mock_client_factory(lambda request: httpx.Response(200, text="not json{{{"))
    with pytest.raises(MalformedResponseError):
        client.chat_completion({"model": "test-model", "messages": []})


def test_missing_choices_raises(mock_client_factory):
    client = mock_client_factory(
        lambda request: httpx.Response(200, json={"id": "x", "model": "m"})
    )
    with pytest.raises(MalformedResponseError):
        client.chat_completion({"model": "test-model", "messages": []})


def test_non_string_content_raises(mock_client_factory):
    client = mock_client_factory(
        lambda request: httpx.Response(
            200,
            json={
                "id": "x",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": None},
                        "finish_reason": "stop",
                    }
                ],
            },
        )
    )
    with pytest.raises(MalformedResponseError):
        client.chat_completion({"model": "test-model", "messages": []})


def test_non_json_error_body_still_maps_by_status(mock_client_factory):
    client = mock_client_factory(
        lambda request: httpx.Response(401, text="<html>bad gateway</html>")
    )
    with pytest.raises(AuthenticationError):
        client.list_models()


def test_model_info_defaults():
    model = ModelInfo(id="m")
    assert model.capabilities == []
    assert model.supports_json_mode is False
    assert model.estimated_cost_per_1k is None
