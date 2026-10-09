"""SIMULATED / OFFLINE CleanAPIs integration tests.

These tests run the **production** CleanAPIs code path against a local,
in-process HTTP simulation of the external CleanAPIs service
(``tests/simulated/cleanapis_server.py``):

    Application (test code using the LLMProvider interface)
      → LLMProvider interface (factory.providers.base)
        → CleanAPIsProvider (production implementation)
          → CleanAPIsClient (production thin HTTP client, real httpx over a
            real socket to 127.0.0.1)
            → simulated CleanAPIs HTTP endpoint (documented wire protocol)
              → response parsing → normalized internal LLMResponse

No real ``cleanapis_API_KEY`` is required or used (the simulation accepts one
clearly-fake key), no real network egress occurs, and no real CleanAPIs
connectivity is claimed. The REAL connectivity test remains available for an
owner-run in ``tests/integration/test_cleanapis_connectivity.py`` (marked
``live``).

Covered simulation surface (per the Phase 0 finalization change):
authentication success/failure · valid model responses · structured JSON
responses · malformed responses · HTTP 4xx · HTTP 5xx · timeouts · retry
behavior · rate limiting · usage/token metadata · invalid/missing
configuration · secret redaction.
"""

from __future__ import annotations

import io
import json
import logging
import time
from typing import Any

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from factory.config.provider_config import CleanAPISSettings
from factory.errors import (
    AuthenticationError,
    BudgetExceededError,
    ConfigurationError,
    InsufficientFundsError,
    MalformedResponseError,
    ModelNotFoundError,
    ProviderConfigurationError,
    ProviderError,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderValidationError,
    RateLimitError,
    ScopeError,
    StructuredOutputError,
)
from factory.observability.logging import JSONFormatter, SecretRedactionFilter
from factory.providers.base import LLMProvider
from factory.providers.budget import BudgetEnforcer, InMemoryBudgetStore, RequestBudget
from factory.providers.cache import InMemoryLLMCache
from factory.providers.cleanapis.client import CleanAPIsClient
from factory.providers.cleanapis.connectivity import run_connectivity_test, select_model
from factory.providers.cleanapis.provider import CleanAPIsProvider
from factory.providers.llm.types import FinishReason, LLMRequest, LLMResponse
from factory.security.redaction import register_secret, unregister_all_secrets
from factory.storage.models import LLMCacheEntry, ProviderUsage
from tests.conftest import ListUsageTracker
from tests.simulated.cleanapis_server import (
    SIMULATED_API_KEY,
    SIMULATED_MODEL_CHEAP,
    SIMULATED_MODEL_PREMIUM,
    SimulatedBehavior,
    SimulatedCleanAPISServer,
)

pytestmark = [pytest.mark.simulated, pytest.mark.offline]

WRONG_API_KEY = "cc_wrong_simulated_key_0000000002"  # fake value, offline only


class Greeting(BaseModel):
    """Schema used for structured-output tests."""

    model_config = {"extra": "forbid"}

    greeting: str = Field(min_length=1)
    language: str = "en"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sim_server():
    """A running local simulation of the CleanAPIs HTTP API."""
    with SimulatedCleanAPISServer() as server:
        yield server


@pytest.fixture
def sim_settings(sim_server: SimulatedCleanAPISServer) -> CleanAPISSettings:
    """Provider settings pointing at the simulated endpoint (fake key)."""
    return CleanAPISSettings(
        cleanapis_API_KEY=SIMULATED_API_KEY,
        base_url=sim_server.base_url,
        model=SIMULATED_MODEL_CHEAP,
        timeout_seconds=5.0,
        max_retries=2,
    )


@pytest.fixture
def usage_tracker() -> ListUsageTracker:
    return ListUsageTracker()


@pytest.fixture
def provider(sim_settings: CleanAPISSettings, usage_tracker: ListUsageTracker) -> LLMProvider:
    """The production provider, typed as the LLMProvider interface."""
    return CleanAPIsProvider(
        sim_settings,
        usage_tracker=usage_tracker,
        cache=InMemoryLLMCache(),
    )


def _request(**kwargs: Any) -> LLMRequest:
    defaults: dict[str, Any] = {
        "model": SIMULATED_MODEL_CHEAP,
        "messages": [{"role": "user", "content": "Say hi"}],
        "temperature": 0.0,
    }
    defaults.update(kwargs)
    return LLMRequest(**defaults)


# ---------------------------------------------------------------------------
# The full production path
# ---------------------------------------------------------------------------


class TestFullProductionPath:
    """Application → LLMProvider → CleanAPIsProvider → simulated HTTP → parse."""

    def test_complete_through_llm_provider_interface(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
        usage_tracker: ListUsageTracker,
    ) -> None:
        assert isinstance(provider, CleanAPIsProvider)
        response = provider.complete(
            _request(max_tokens=64),
            job_id="job-sim-1",
            purpose="simulated-integration",
        )
        # Normalized internal response:
        assert isinstance(response, LLMResponse)
        assert response.content == "Hello from the simulated CleanAPIs service."
        assert response.provider == "cleanapis"
        assert response.model == SIMULATED_MODEL_CHEAP
        assert response.usage.prompt_tokens == 11
        assert response.usage.completion_tokens == 7
        assert response.usage.total_tokens == 18
        assert response.finish_reason == FinishReason.STOP
        assert response.latency_ms >= 0
        assert response.cached is False
        assert response.metadata["usage_available"] is True
        assert response.metadata["rate_limits"]["limit_requests"] == 300
        assert response.metadata["rate_limits"]["remaining_tokens"] == 499982

        # The simulated service received exactly one well-formed request:
        assert sim_server.state.chat_request_count == 1
        sent = sim_server.state.chat_bodies[0]
        assert sent["model"] == SIMULATED_MODEL_CHEAP
        assert sent["messages"] == [{"role": "user", "content": "Say hi"}]
        assert sent["max_tokens"] == 64
        assert sent["temperature"] == 0.0
        assert "response_format" not in sent

        # Usage was recorded through the production tracking path:
        assert len(usage_tracker.records) == 1
        record = usage_tracker.records[0]
        assert record.provider == "cleanapis"
        assert record.model == SIMULATED_MODEL_CHEAP
        assert record.success is True
        assert record.total_tokens == 18
        assert record.usage_available is True
        assert record.job_id == "job-sim-1"
        assert record.purpose == "simulated-integration"
        assert record.request_id == "chatcmpl-simulated-0001"

    def test_complete_structured_through_llm_provider_interface(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(content=json.dumps({"greeting": "hello", "language": "en"}))
        )
        result = provider.complete_structured(_request(), Greeting, purpose="simulated-structured")
        assert isinstance(result, Greeting)
        assert result.greeting == "hello"
        # json_mode was requested on the wire:
        assert sim_server.state.chat_bodies[0]["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# 1. Authentication success/failure
# ---------------------------------------------------------------------------


class TestAuthentication:
    def test_valid_key_is_accepted(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        provider.complete(_request())
        assert sim_server.state.chat_request_count == 1
        assert all(r.authorized for r in sim_server.state.requests)

    def test_invalid_key_rejected_with_401(
        self,
        sim_server: SimulatedCleanAPISServer,
        sim_settings: CleanAPISSettings,
        usage_tracker: ListUsageTracker,
    ) -> None:
        bad_settings = sim_settings.model_copy(update={"cleanapis_API_KEY": WRONG_API_KEY})
        bad_provider = CleanAPIsProvider(
            bad_settings, usage_tracker=usage_tracker, cache=InMemoryLLMCache()
        )
        with pytest.raises(AuthenticationError) as exc_info:
            bad_provider.complete(_request(), job_id="job-auth")
        assert exc_info.value.status_code == 401
        assert exc_info.value.error_code == "invalid_api_key"
        # The simulated service saw the unauthorized attempt:
        assert any(not r.authorized for r in sim_server.state.requests)
        # The failure was recorded with its error type:
        assert usage_tracker.records[-1].success is False
        assert usage_tracker.records[-1].error_type == "AuthenticationError"

    def test_missing_key_fails_safely_before_any_request(
        self, sim_server: SimulatedCleanAPISServer
    ) -> None:
        settings = CleanAPISSettings(cleanapis_API_KEY=None, base_url=sim_server.base_url)
        with pytest.raises(ConfigurationError):
            settings.require_api_key()
        with pytest.raises(ProviderConfigurationError):
            CleanAPIsClient(settings)
        with pytest.raises(ProviderConfigurationError):
            CleanAPIsProvider(settings)
        # No request ever reached the simulated service:
        assert sim_server.state.request_count == 0


# ---------------------------------------------------------------------------
# 2. Valid model response
# ---------------------------------------------------------------------------


class TestModelListing:
    def test_list_models_parses_documented_shape(
        self, sim_server: SimulatedCleanAPISServer, sim_settings: CleanAPISSettings
    ) -> None:
        client = CleanAPIsClient(sim_settings)
        try:
            models = client.list_models()
        finally:
            client.close()
        assert {m.id for m in models} == {SIMULATED_MODEL_CHEAP, SIMULATED_MODEL_PREMIUM}
        cheap = next(m for m in models if m.id == SIMULATED_MODEL_CHEAP)
        assert cheap.context_window == 32000
        assert cheap.supports_json_mode is True
        assert cheap.estimated_cost_per_1k == pytest.approx(0.0003)
        # Cheapest-model selection works on the simulated pricing:
        selected, source = select_model(models, None)
        assert selected.id == SIMULATED_MODEL_CHEAP
        assert source == "cheapest_listed"

    def test_provider_list_models(
        self, sim_settings: CleanAPISSettings, usage_tracker: ListUsageTracker
    ) -> None:
        provider = CleanAPIsProvider(
            sim_settings, usage_tracker=usage_tracker, cache=InMemoryLLMCache()
        )
        models = provider.list_models()
        assert len(models) == 2


# ---------------------------------------------------------------------------
# 3. Structured JSON response (+ 4. malformed response)
# ---------------------------------------------------------------------------


class TestStructuredOutput:
    def test_markdown_fences_tolerated(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(SimulatedBehavior(content='```json\n{"greeting": "fenced"}\n```'))
        result = provider.complete_structured(_request(), Greeting)
        assert result.greeting == "fenced"

    def test_invalid_json_triggers_one_correction_retry(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(content="this is not json"),
            SimulatedBehavior(content=json.dumps({"greeting": "fixed"})),
        )
        result = provider.complete_structured(_request(), Greeting)
        assert result.greeting == "fixed"
        # Two HTTP round trips: initial attempt + one correction retry.
        assert sim_server.state.chat_request_count == 2
        # The correction request carries the rejected output and the reason:
        correction_body = sim_server.state.chat_bodies[1]
        roles = [m["role"] for m in correction_body["messages"]]
        assert roles == ["user", "assistant", "user"]

    def test_persistent_invalid_output_raises(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(content="still not json"),
            SimulatedBehavior(content="and again not json"),
        )
        with pytest.raises(StructuredOutputError):
            provider.complete_structured(_request(), Greeting)
        assert sim_server.state.chat_request_count == 2


class TestMalformedResponses:
    @pytest.mark.parametrize(
        "mode", ["invalid_json", "missing_choices", "empty_choices", "non_string_content"]
    )
    def test_malformed_response_rejected(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider, mode: str
    ) -> None:
        sim_server.state.queue(SimulatedBehavior(mode=mode))
        with pytest.raises(MalformedResponseError):
            provider.complete(_request())
        # Malformed responses are never retried:
        assert sim_server.state.chat_request_count == 1


# ---------------------------------------------------------------------------
# 5. HTTP 4xx errors
# ---------------------------------------------------------------------------


class TestHttp4xx:
    @pytest.mark.parametrize(
        ("status", "error_type"),
        [
            (402, InsufficientFundsError),
            (403, ScopeError),
            (404, ModelNotFoundError),
            (422, ProviderValidationError),
        ],
    )
    def test_4xx_maps_to_typed_error_and_is_not_retried(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
        status: int,
        error_type: type[ProviderError],
    ) -> None:
        sim_server.state.queue(SimulatedBehavior(status=status))
        with pytest.raises(error_type):
            provider.complete(_request())
        assert sim_server.state.chat_request_count == 1  # 4xx is never retried


# ---------------------------------------------------------------------------
# 6. HTTP 5xx errors + 8. retry behavior
# ---------------------------------------------------------------------------


class TestHttp5xxAndRetries:
    @pytest.mark.parametrize("status", [500, 502])
    def test_5xx_retried_then_raises(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
        status: int,
    ) -> None:
        # Default max_retries=2 → 1 initial attempt + 2 retries = 3 requests.
        sim_server.state.queue(
            SimulatedBehavior(status=status),
            SimulatedBehavior(status=status),
            SimulatedBehavior(status=status),
        )
        with pytest.raises(ProviderHTTPError) as exc_info:
            provider.complete(_request())
        assert exc_info.value.status_code == status
        assert sim_server.state.chat_request_count == 3

    def test_5xx_then_success_recovers(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(status=500),
            SimulatedBehavior(content="recovered"),
        )
        response = provider.complete(_request())
        assert response.content == "recovered"
        assert sim_server.state.chat_request_count == 2

    def test_rate_limit_retries_then_succeeds(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(status=429, retry_after=0.1),
            SimulatedBehavior(status=429, retry_after=0.1),
            SimulatedBehavior(content="after rate limit"),
        )
        response = provider.complete(_request())
        assert response.content == "after rate limit"
        assert sim_server.state.chat_request_count == 3

    def test_rate_limit_retries_exhausted_raises(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(status=429, retry_after=1),
            SimulatedBehavior(status=429, retry_after=1),
            SimulatedBehavior(status=429, retry_after=1),
        )
        started = time.monotonic()
        with pytest.raises(RateLimitError) as exc_info:
            provider.complete(_request())
        elapsed = time.monotonic() - started
        assert sim_server.state.chat_request_count == 3
        assert exc_info.value.retry_after_seconds == 1.0
        # Retry-After delays were honored (two retries of 1s each):
        assert elapsed >= 1.8


# ---------------------------------------------------------------------------
# 7. Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_slow_response_times_out(
        self,
        sim_server: SimulatedCleanAPISServer,
        sim_settings: CleanAPISSettings,
        usage_tracker: ListUsageTracker,
    ) -> None:
        fast_settings = sim_settings.model_copy(update={"timeout_seconds": 0.5})
        provider = CleanAPIsProvider(
            fast_settings, usage_tracker=usage_tracker, cache=InMemoryLLMCache()
        )
        sim_server.state.queue(SimulatedBehavior(delay_seconds=2.0))
        with pytest.raises(ProviderTimeoutError):
            provider.complete(_request(), job_id="job-timeout")
        # The timeout failure was recorded:
        record = usage_tracker.records[-1]
        assert record.success is False
        assert record.error_type == "ProviderTimeoutError"


# ---------------------------------------------------------------------------
# 9. Rate-limit response
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_rate_limit_error_carries_retry_after(
        self, sim_server: SimulatedCleanAPISServer, provider: LLMProvider
    ) -> None:
        sim_server.state.queue(
            SimulatedBehavior(status=429, retry_after=5),
            SimulatedBehavior(status=429, retry_after=5),
            SimulatedBehavior(status=429, retry_after=5),
        )
        with pytest.raises(RateLimitError) as exc_info:
            provider.complete(_request())
        assert exc_info.value.retry_after_seconds == 5.0

    def test_rate_limit_headers_exposed_on_success(self, provider: LLMProvider) -> None:
        response = provider.complete(_request())
        limits = response.metadata["rate_limits"]
        assert limits == {
            "limit_requests": 300,
            "remaining_requests": 299,
            "limit_tokens": 500000,
            "remaining_tokens": 499982,
        }


# ---------------------------------------------------------------------------
# 10. Usage / token metadata
# ---------------------------------------------------------------------------


class TestUsageMetadata:
    def test_success_records_full_token_metadata(
        self, provider: LLMProvider, usage_tracker: ListUsageTracker
    ) -> None:
        response = provider.complete(_request(), job_id="job-usage", purpose="sim")
        assert response.usage.total_tokens == 18
        record = usage_tracker.records[-1]
        assert record.prompt_tokens == 11
        assert record.completion_tokens == 7
        assert record.total_tokens == 18
        assert record.success is True
        assert record.usage_available is True
        assert record.latency_ms is not None and record.latency_ms >= 0

    def test_failure_records_error_without_tokens(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
        usage_tracker: ListUsageTracker,
    ) -> None:
        sim_server.state.queue(SimulatedBehavior(status=402))
        with pytest.raises(InsufficientFundsError):
            provider.complete(_request(), job_id="job-usage-fail")
        record = usage_tracker.records[-1]
        assert record.success is False
        assert record.error_type == "InsufficientFundsError"
        assert record.total_tokens is None
        assert record.usage_available is False

    def test_unreported_usage_is_recorded_as_unavailable_not_estimated(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
        usage_tracker: ListUsageTracker,
    ) -> None:
        sim_server.state.queue(SimulatedBehavior(include_usage=False))
        response = provider.complete(_request())
        # The provider did not report usage → None, never estimated:
        assert response.usage.prompt_tokens is None
        assert response.usage.completion_tokens is None
        assert response.usage.total_tokens is None
        assert response.metadata["usage_available"] is False
        record = usage_tracker.records[-1]
        assert record.usage_available is False
        assert record.total_tokens is None

    def test_database_backed_usage_and_cache(
        self,
        sim_server: SimulatedCleanAPISServer,
        sim_settings: CleanAPISSettings,
        session_factory: Any,
    ) -> None:
        provider = CleanAPIsProvider.with_sqlite_cache(sim_settings, session_factory)
        request = _request()
        provider.complete(request, job_id="job-db", purpose="sim")
        with session_factory() as session:
            rows = session.execute(select(ProviderUsage)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.provider == "cleanapis"
        assert row.model == SIMULATED_MODEL_CHEAP
        assert row.total_tokens == 18
        assert row.success is True
        assert row.usage_available is True
        assert row.job_id == "job-db"

        # A second identical deterministic request is served from the
        # database-backed cache without hitting the simulated service again:
        cached = provider.complete(request, job_id="job-db")
        assert cached.cached is True
        assert sim_server.state.chat_request_count == 1
        with session_factory() as session:
            cache_rows = session.execute(select(LLMCacheEntry)).scalars().all()
        assert len(cache_rows) == 1
        assert cache_rows[0].hit_count == 1
        assert cache_rows[0].provider == "cleanapis"

    def test_cache_dedup_and_non_deterministic_bypass(
        self,
        sim_server: SimulatedCleanAPISServer,
        provider: LLMProvider,
    ) -> None:
        deterministic = _request()
        first = provider.complete(deterministic)
        second = provider.complete(deterministic)
        assert first.cached is False
        assert second.cached is True
        assert sim_server.state.chat_request_count == 1  # deduplicated

        stochastic = _request(temperature=0.7)
        provider.complete(stochastic)
        provider.complete(stochastic)
        assert sim_server.state.chat_request_count == 3  # not cached

    def test_budget_enforced_before_any_http_call(
        self,
        sim_server: SimulatedCleanAPISServer,
        sim_settings: CleanAPISSettings,
        usage_tracker: ListUsageTracker,
    ) -> None:
        store = InMemoryBudgetStore()
        for _ in range(3):
            store.add("job-budget", provider="cleanapis", total_tokens=10)
        enforcer = BudgetEnforcer(store, RequestBudget(max_requests=3))
        provider = CleanAPIsProvider(
            sim_settings,
            usage_tracker=usage_tracker,
            cache=InMemoryLLMCache(),
            budget_enforcer=enforcer,
        )
        with pytest.raises(BudgetExceededError):
            provider.complete(_request(), job_id="job-budget")
        assert sim_server.state.chat_request_count == 0  # provider never called


# ---------------------------------------------------------------------------
# 11. Invalid / missing configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_base_url_must_end_with_v1(self, sim_server: SimulatedCleanAPISServer) -> None:
        with pytest.raises(ConfigurationError):
            CleanAPISSettings(
                cleanapis_API_KEY=SIMULATED_API_KEY,
                base_url=f"{sim_server.base_url}/chat/completions",
            )

    def test_base_url_must_be_http(self) -> None:
        with pytest.raises(ConfigurationError):
            CleanAPISSettings(cleanapis_API_KEY=SIMULATED_API_KEY, base_url="ftp://example.com/v1")

    def test_key_with_whitespace_rejected(self, sim_server: SimulatedCleanAPISServer) -> None:
        with pytest.raises(ConfigurationError):
            CleanAPISSettings(
                cleanapis_API_KEY="cc_has whitespace",
                base_url=sim_server.base_url,
            )

    def test_missing_default_model_and_request_model(
        self,
        sim_server: SimulatedCleanAPISServer,
        usage_tracker: ListUsageTracker,
    ) -> None:
        settings = CleanAPISSettings(
            cleanapis_API_KEY=SIMULATED_API_KEY,
            base_url=sim_server.base_url,
            model=None,
        )
        provider = CleanAPIsProvider(
            settings, usage_tracker=usage_tracker, cache=InMemoryLLMCache()
        )
        # No default model and none in the request → safe configuration error:
        with pytest.raises(ConfigurationError):
            provider.complete(LLMRequest(messages=[{"role": "user", "content": "hi"}]))
        # An explicit request model works:
        response = provider.complete(_request())
        assert response.model == SIMULATED_MODEL_CHEAP


# ---------------------------------------------------------------------------
# 12. Secret redaction
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_simulated_key_never_leaks(
        self,
        sim_server: SimulatedCleanAPISServer,
        sim_settings: CleanAPISSettings,
        usage_tracker: ListUsageTracker,
    ) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretRedactionFilter())
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        register_secret(SIMULATED_API_KEY)
        register_secret(WRONG_API_KEY)
        try:
            provider = CleanAPIsProvider(
                sim_settings, usage_tracker=usage_tracker, cache=InMemoryLLMCache()
            )
            provider.complete(_request(), job_id="job-secret", purpose="redaction")

            bad_settings = sim_settings.model_copy(update={"cleanapis_API_KEY": WRONG_API_KEY})
            bad_provider = CleanAPIsProvider(
                bad_settings, usage_tracker=usage_tracker, cache=InMemoryLLMCache()
            )
            with pytest.raises(AuthenticationError) as exc_info:
                bad_provider.complete(_request())

            # Simulate an accidental leak attempt: a log line containing the
            # key must be redacted by the production filter + formatter.
            from factory.observability.logging import get_logger

            get_logger("factory.providers.cleanapis.provider").info(
                "debug dump: api key is %s", SIMULATED_API_KEY
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)
            unregister_all_secrets()

        output = stream.getvalue()
        # Logs never contain the keys (registered-secret and pattern redaction):
        assert SIMULATED_API_KEY not in output
        assert WRONG_API_KEY not in output
        assert "[REDACTED]" in output  # the redaction layer was active
        # Exceptions never contain the keys:
        assert SIMULATED_API_KEY not in str(exc_info.value)
        assert WRONG_API_KEY not in str(exc_info.value)
        # Usage records never contain the keys:
        blob = json.dumps([r.to_dict() for r in usage_tracker.records])
        assert SIMULATED_API_KEY not in blob
        assert WRONG_API_KEY not in blob
        assert "authorization" not in blob.lower()


# ---------------------------------------------------------------------------
# The real connectivity-test code path, against the simulation
# ---------------------------------------------------------------------------


class TestSimulatedConnectivityTest:
    """The production connectivity test (run_connectivity_test) exercised
    against the simulated service — the same code the owner will later run
    against the real API."""

    def test_connectivity_test_passes_against_simulation(
        self, sim_server: SimulatedCleanAPISServer, sim_settings: CleanAPISSettings
    ) -> None:
        report = run_connectivity_test(sim_settings, timeout_seconds=5.0)
        assert report.overall_pass is True
        assert [step.name for step in report.steps] == [
            "api_key_available",
            "endpoint_reachable_and_authenticated",
            "model_available",
            "minimal_request_succeeds",
            "response_parseable",
            "errors_handled_safely",
        ]
        assert all(step.passed for step in report.steps)
        # The configured model exists in the simulated listing → used:
        assert report.model_selected == SIMULATED_MODEL_CHEAP
        assert report.model_source == "configured"
        # The report never contains the key:
        assert SIMULATED_API_KEY not in json.dumps(report.to_dict())

    def test_connectivity_test_selects_cheapest_when_unconfigured(
        self, sim_server: SimulatedCleanAPISServer, sim_settings: CleanAPISSettings
    ) -> None:
        settings = sim_settings.model_copy(update={"model": None})
        report = run_connectivity_test(settings, timeout_seconds=5.0)
        assert report.overall_pass is True
        # No configured model → cheapest by simulated reported pricing:
        assert report.model_selected == SIMULATED_MODEL_CHEAP
        assert report.model_source == "cheapest_listed"

    def test_connectivity_test_reports_auth_failure_safely(
        self, sim_server: SimulatedCleanAPISServer, sim_settings: CleanAPISSettings
    ) -> None:
        bad_settings = sim_settings.model_copy(update={"cleanapis_API_KEY": WRONG_API_KEY})
        report = run_connectivity_test(bad_settings, timeout_seconds=5.0)
        assert report.overall_pass is False
        auth_step = report.steps[1]
        assert auth_step.name == "endpoint_reachable_and_authenticated"
        assert auth_step.passed is False
        assert auth_step.error_type == "AuthenticationError"
        # Sanitized: no key material in the report:
        assert WRONG_API_KEY not in json.dumps(report.to_dict())

    def test_connectivity_test_reports_missing_key_safely(
        self, sim_server: SimulatedCleanAPISServer
    ) -> None:
        settings = CleanAPISSettings(cleanapis_API_KEY=None, base_url=sim_server.base_url)
        report = run_connectivity_test(settings, timeout_seconds=5.0)
        assert report.overall_pass is False
        assert report.steps[0].error_type == "ConfigurationError"
        assert sim_server.state.request_count == 0  # nothing was sent
