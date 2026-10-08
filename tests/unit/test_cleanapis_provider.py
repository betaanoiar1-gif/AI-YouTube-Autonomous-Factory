"""CleanAPIsProvider tests — normalized request/response, usage tracking,
caching/deduplication, budgets, and structured output (all mocked HTTP)."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from factory.errors import (
    BudgetExceededError,
    ConfigurationError,
    StructuredOutputError,
)
from factory.providers.budget import BudgetEnforcer, InMemoryBudgetStore, RequestBudget
from factory.providers.llm.types import FinishReason, LLMRequest
from tests.conftest import ListUsageTracker, chat_completion_response, error_response


def _request(**kwargs) -> LLMRequest:
    defaults = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Say hi"}],
        "temperature": 0.0,
    }
    defaults.update(kwargs)
    return LLMRequest(**defaults)


class Greeting(BaseModel):
    model_config = {"extra": "forbid"}

    greeting: str = Field(min_length=1)
    language: str = "en"


class TestComplete:
    def test_success_normalizes_response(self, provider_factory):
        provider, usage = provider_factory(lambda request: chat_completion_response(content="Hi!"))
        response = provider.complete(_request(), job_id="job-1", purpose="test")
        assert response.content == "Hi!"
        assert response.provider == "cleanapis"
        assert response.model == "test-model"
        assert response.usage.total_tokens == 15
        assert response.finish_reason == FinishReason.STOP
        assert response.latency_ms >= 0
        assert response.cached is False
        assert response.metadata["rate_limits"]["remaining_requests"] == 299

        assert len(usage.records) == 1
        record = usage.records[0]
        assert record.provider == "cleanapis"
        assert record.model == "test-model"
        assert record.success is True
        assert record.total_tokens == 15
        assert record.job_id == "job-1"
        assert record.purpose == "test"
        assert record.request_id == "chatcmpl-test-1"

    def test_payload_built_from_normalized_request(self, provider_factory):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return chat_completion_response()

        provider, _ = provider_factory(handler)
        provider.complete(
            _request(
                system="You are terse.",
                max_tokens=128,
                top_p=0.9,
                stop=["END"],
                seed=42,
            )
        )
        payload = seen[0]
        assert payload["model"] == "test-model"
        assert payload["messages"][0] == {"role": "system", "content": "You are terse."}
        assert payload["messages"][1] == {"role": "user", "content": "Say hi"}
        assert payload["temperature"] == 0.0
        assert payload["max_tokens"] == 128
        assert payload["top_p"] == 0.9
        assert payload["stop"] == ["END"]
        assert payload["seed"] == 42
        assert "response_format" not in payload

    def test_json_mode_adds_response_format(self, provider_factory):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return chat_completion_response()

        provider, _ = provider_factory(handler)
        provider.complete(_request(json_mode=True))
        assert seen[0]["response_format"] == {"type": "json_object"}

    def test_provider_default_model_used_when_request_has_none(self, provider_factory):
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return chat_completion_response()

        provider, _ = provider_factory(handler)
        provider.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}]))
        assert seen[0]["model"] == "test-model"

    def test_missing_default_model_raises_configuration_error(self):
        from factory.config.provider_config import CleanAPISSettings
        from factory.providers.cleanapis.provider import CleanAPIsProvider

        settings = CleanAPISSettings(cleanapis_API_KEY="cc_test_x", model=None)
        provider = CleanAPIsProvider(settings, usage_tracker=ListUsageTracker())
        with pytest.raises(ConfigurationError):
            provider.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}]))

    def test_failure_records_usage_with_error_type(self, provider_factory):
        provider, usage = provider_factory(
            lambda request: error_response(
                401, "bad key", "authentication_error", "invalid_api_key"
            )
        )
        from factory.errors import AuthenticationError

        with pytest.raises(AuthenticationError):
            provider.complete(_request(), job_id="job-9", purpose="test")
        assert len(usage.records) == 1
        record = usage.records[0]
        assert record.success is False
        assert record.error_type == "AuthenticationError"
        assert record.job_id == "job-9"
        assert record.total_tokens is None
        assert record.usage_available is False

    def test_usage_record_never_contains_api_key(self, provider_factory, cleanapis_settings):
        provider, usage = provider_factory(lambda request: chat_completion_response())
        provider.complete(_request())
        blob = json.dumps([r.to_dict() for r in usage.records])
        assert cleanapis_settings.cleanapis_API_KEY not in blob
        assert "authorization" not in blob.lower()


class TestCaching:
    def test_deterministic_request_cached(self, provider_factory):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return chat_completion_response(content="cached answer")

        provider, _ = provider_factory(handler)
        first = provider.complete(_request())
        second = provider.complete(_request())
        assert len(calls) == 1  # second call served from cache
        assert first.content == second.content == "cached answer"
        assert second.cached is True

    def test_non_deterministic_request_not_cached(self, provider_factory):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return chat_completion_response()

        provider, _ = provider_factory(handler)
        provider.complete(_request(temperature=0.7))
        provider.complete(_request(temperature=0.7))
        assert len(calls) == 2

    def test_cache_disabled_per_request(self, provider_factory):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return chat_completion_response()

        provider, _ = provider_factory(handler)
        provider.complete(_request(use_cache=False))
        provider.complete(_request(use_cache=False))
        assert len(calls) == 2


class TestBudget:
    def test_budget_exceeded_blocks_request(self, provider_factory):
        store = InMemoryBudgetStore()
        # Simulate 3 already-spent requests for the job.
        for _ in range(3):
            store.add("job-budget", provider="cleanapis", total_tokens=100)
        enforcer = BudgetEnforcer(store, RequestBudget(max_requests=3))
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return chat_completion_response()

        provider, _ = provider_factory(handler, budget_enforcer=enforcer)
        with pytest.raises(BudgetExceededError):
            provider.complete(_request(), job_id="job-budget")
        assert calls == []  # provider never called

    def test_token_budget_exceeded(self, provider_factory):
        store = InMemoryBudgetStore()
        store.add("job-tokens", provider="cleanapis", total_tokens=6_000_000)
        enforcer = BudgetEnforcer(
            store, RequestBudget(max_requests=100, max_total_tokens=5_000_000)
        )
        provider, _ = provider_factory(
            lambda request: chat_completion_response(), budget_enforcer=enforcer
        )
        with pytest.raises(BudgetExceededError):
            provider.complete(_request(), job_id="job-tokens")


class TestStructuredOutput:
    def test_valid_json_validated(self, provider_factory):
        provider, _ = provider_factory(
            lambda request: chat_completion_response(
                content=json.dumps({"greeting": "hello", "language": "en"})
            )
        )
        result = provider.complete_structured(_request(), Greeting, purpose="test")
        assert result.greeting == "hello"

    def test_markdown_fences_tolerated(self, provider_factory):
        provider, _ = provider_factory(
            lambda request: chat_completion_response(content='```json\n{"greeting": "hi"}\n```')
        )
        result = provider.complete_structured(_request(), Greeting)
        assert result.greeting == "hi"

    def test_invalid_json_triggers_one_correction_retry(self, provider_factory):
        responses = iter(
            [
                chat_completion_response(content="not json at all"),
                chat_completion_response(content=json.dumps({"greeting": "fixed"})),
            ]
        )
        provider, usage = provider_factory(lambda request: next(responses))
        result = provider.complete_structured(_request(), Greeting)
        assert result.greeting == "fixed"
        assert len(usage.records) == 2  # both attempts tracked

    def test_schema_violation_triggers_correction_retry(self, provider_factory):
        responses = iter(
            [
                chat_completion_response(
                    content=json.dumps({"greeting": ""})
                ),  # violates min_length
                chat_completion_response(content=json.dumps({"greeting": "ok"})),
            ]
        )
        provider, _ = provider_factory(lambda request: next(responses))
        result = provider.complete_structured(_request(), Greeting)
        assert result.greeting == "ok"

    def test_persistent_failure_raises_structured_output_error(self, provider_factory):
        provider, _ = provider_factory(
            lambda request: chat_completion_response(content="still not json")
        )
        with pytest.raises(StructuredOutputError):
            provider.complete_structured(_request(), Greeting)

    def test_extra_fields_rejected_by_schema(self, provider_factory):
        provider, _ = provider_factory(
            lambda request: chat_completion_response(
                content=json.dumps({"greeting": "hi", "unexpected": True})
            )
        )
        # First attempt invalid (extra field), correction retry also invalid.
        with pytest.raises(StructuredOutputError):
            provider.complete_structured(_request(), Greeting)
