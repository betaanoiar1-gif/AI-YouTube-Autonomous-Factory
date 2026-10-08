"""CleanAPIsProvider — the real LLMProvider implementation for Phase 0.

Responsibilities:

* translate normalized :class:`LLMRequest` / :class:`LLMResponse` to/from the
  verified CleanAPIs wire format;
* track usage for EVERY request (success or failure) via the UsageTracker —
  the API key is never recorded;
* deduplicate deterministic requests through the response cache;
* enforce per-job request/token budgets before calling the provider;
* never log or expose the API key.

Business logic depends on the :class:`LLMProvider` interface; swapping
providers does not require touching callers.
"""

from __future__ import annotations

import time
from typing import Any

from factory.config.provider_config import CleanAPISSettings
from factory.errors import (
    BudgetExceededError,
    ConfigurationError,
    ProviderError,
)
from factory.observability.logging import bind_context, clear_context, get_logger
from factory.observability.usage import LoggingUsageTracker, ProviderUsageRecord, UsageTracker
from factory.providers.base import LLMProvider
from factory.providers.budget import BudgetEnforcer, RequestBudget
from factory.providers.cache import (
    InMemoryLLMCache,
    LLMResponseCache,
    SQLiteLLMCache,
    compute_cache_key,
)
from factory.providers.cleanapis.client import PROVIDER_NAME, ChatCompletionResult, CleanAPIsClient
from factory.providers.llm.types import FinishReason, LLMRequest, LLMResponse, LLMUsage

logger = get_logger(__name__)


class CleanAPIsProvider(LLMProvider):
    """LLMProvider implementation backed by the CleanAPIs API."""

    name = PROVIDER_NAME

    def __init__(
        self,
        settings: CleanAPISSettings,
        *,
        client: CleanAPIsClient | None = None,
        usage_tracker: UsageTracker | None = None,
        cache: LLMResponseCache | None = None,
        budget: RequestBudget | None = None,
        budget_enforcer: BudgetEnforcer | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or CleanAPIsClient(settings)
        self._usage_tracker = usage_tracker or LoggingUsageTracker()
        self._cache: LLMResponseCache | None
        if cache is not None:
            self._cache = cache
        elif settings.cache_enabled:
            self._cache = InMemoryLLMCache()
        else:
            self._cache = None
        self._budget = budget
        self._budget_enforcer = budget_enforcer

    @classmethod
    def with_sqlite_cache(
        cls,
        settings: CleanAPISSettings,
        session_factory: Any,
        **kwargs: Any,
    ) -> CleanAPIsProvider:
        """Build a provider whose cache and usage tracking use the database."""
        from factory.observability.usage import SQLAlchemyUsageTracker

        kwargs.setdefault("cache", SQLiteLLMCache(session_factory))
        kwargs.setdefault("usage_tracker", SQLAlchemyUsageTracker(session_factory))
        return cls(settings, **kwargs)

    @property
    def default_model(self) -> str:
        if not self._settings.model:
            raise ConfigurationError(
                "No default CleanAPIs model configured (set CLEANAPIS_MODEL) and the "
                "request did not specify one"
            )
        return self._settings.model

    def list_models(self) -> list[Any]:
        """List available models (also used by the connectivity test)."""
        return self._client.list_models()

    def complete(
        self,
        request: LLMRequest,
        *,
        job_id: str | None = None,
        purpose: str | None = None,
    ) -> LLMResponse:
        """Run a completion with usage tracking, caching, and budget checks."""
        model = request.model or self.default_model
        token = bind_context(provider=PROVIDER_NAME, model=model, job_id=job_id, purpose=purpose)
        try:
            self._enforce_budget(job_id=job_id)
            cache_key: str | None = None
            if self._cache is not None and self._is_cacheable(request):
                cache_key = compute_cache_key(request, provider=PROVIDER_NAME, model=model)
                cached = self._cache.get(cache_key)
                if cached is not None:
                    logger.info("llm_cache_hit", extra={"cache_key": cache_key[:16]})
                    return cached.model_copy(update={"cached": True})

            payload = self._build_payload(request, model)
            started = time.monotonic()
            try:
                result = self._client.chat_completion(payload)
                latency_ms = int((time.monotonic() - started) * 1000)
                response = self._normalize(result, model=model, latency_ms=latency_ms)
            except ProviderError as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                self._record_usage(
                    model=model,
                    latency_ms=latency_ms,
                    usage=None,
                    success=False,
                    error_type=type(exc).__name__,
                    job_id=job_id,
                    purpose=purpose,
                    request_id=None,
                )
                raise
            self._record_usage(
                model=model,
                latency_ms=latency_ms,
                usage=response.usage,
                success=True,
                error_type=None,
                job_id=job_id,
                purpose=purpose,
                request_id=response.request_id,
            )
            if cache_key is not None and self._cache is not None:
                ttl = request.cache_ttl_seconds or self._settings.cache_ttl_seconds
                self._cache.set(cache_key, response, ttl_seconds=ttl)
            return response
        finally:
            clear_context(token)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_cacheable(request: LLMRequest) -> bool:
        """Only deterministic requests (temperature 0, opted in) are cached."""
        return request.use_cache and request.temperature == 0

    def _enforce_budget(self, *, job_id: str | None) -> None:
        if self._budget_enforcer is None or job_id is None:
            return
        try:
            self._budget_enforcer.check(job_id=job_id, provider=PROVIDER_NAME)
        except BudgetExceededError:
            logger.warning("llm_budget_exceeded", extra={"job_id": job_id})
            raise

    @staticmethod
    def _build_payload(request: LLMRequest, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    **({"name": m.name} if m.name else {}),
                    **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
                }
                for m in request.effective_messages()
            ],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop is not None:
            payload["stop"] = request.stop
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _normalize(result: ChatCompletionResult, *, model: str, latency_ms: int) -> LLMResponse:
        usage = LLMUsage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
        )
        usage_available = result.total_tokens is not None
        return LLMResponse(
            content=result.content,
            provider=PROVIDER_NAME,
            model=result.model or model,
            usage=usage,
            finish_reason=FinishReason.from_provider(result.finish_reason),
            latency_ms=latency_ms,
            request_id=result.id,
            cached=False,
            metadata={
                "usage_available": usage_available,
                "rate_limits": {
                    "limit_requests": result.rate_limits.limit_requests,
                    "remaining_requests": result.rate_limits.remaining_requests,
                    "limit_tokens": result.rate_limits.limit_tokens,
                    "remaining_tokens": result.rate_limits.remaining_tokens,
                },
            },
        )

    def _record_usage(
        self,
        *,
        model: str,
        latency_ms: int,
        usage: LLMUsage | None,
        success: bool,
        error_type: str | None,
        job_id: str | None,
        purpose: str | None,
        request_id: str | None,
    ) -> None:
        """Record a provider usage observation. Never records the API key."""
        record = ProviderUsageRecord(
            provider=PROVIDER_NAME,
            model=model,
            latency_ms=latency_ms,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            success=success,
            error_type=error_type,
            job_id=job_id,
            purpose=purpose,
            request_id=request_id,
            usage_available=usage is not None and usage.total_tokens is not None,
        )
        try:
            self._usage_tracker.record(record)
        except Exception:
            logger.exception("usage_recording_failed")
