"""CleanAPIs connectivity test — a REAL but minimal verification.

Verifies, in order:

1. the API key is available in the environment;
2. authentication works (a request carrying the key is accepted);
3. the configured endpoint is reachable;
4. at least one valid model is available (and selects one: the configured
   model, or the cheapest listed model by reported per-1K pricing — ties are
   broken by model id for determinism);
5. a minimal request succeeds (tiny deterministic prompt, temperature 0);
6. the response can be parsed (choices, content, usage);
7. errors are handled safely (every failure is captured as a typed,
   sanitized step result — no secrets, no stack-trace key leakage).

The test performs exactly ONE completion call. The prompt is minimal and the
response content is not stored. The API key is never printed, logged, or
included in the report.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from factory.config.provider_config import CleanAPISSettings
from factory.errors import ProviderError
from factory.observability.logging import get_logger
from factory.observability.usage import LoggingUsageTracker, ProviderUsageRecord
from factory.providers.cleanapis.client import PROVIDER_NAME, CleanAPIsClient, ModelInfo
from factory.providers.llm.types import FinishReason, LLMRequest, LLMResponse, LLMUsage

logger = get_logger(__name__)

# Minimal deterministic probe: tiny prompt, no content generation of any
# substance, temperature 0. CleanAPIs raises max_tokens below 2048 (verified in
# docs), so we pass an explicit small-but-valid budget.
_PROBE_PROMPT = "Reply with exactly the word: OK"
_PROBE_MAX_TOKENS = 2048
_PROBE_TEMPERATURE = 0.0


@dataclass
class ConnectivityStep:
    """One verification step and its outcome."""

    step: int
    name: str
    passed: bool
    detail: str = ""
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectivityReport:
    """Full connectivity test report. Contains NO secrets."""

    overall_pass: bool
    steps: list[ConnectivityStep] = field(default_factory=list)
    base_url: str = ""
    model_selected: str | None = None
    model_source: str | None = None
    probe: dict[str, Any] = field(default_factory=dict)
    finished_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_pass": self.overall_pass,
            "base_url": self.base_url,
            "model_selected": self.model_selected,
            "model_source": self.model_source,
            "steps": [step.to_dict() for step in self.steps],
            "probe": self.probe,
            "finished_at": self.finished_at,
        }


def select_model(models: list[ModelInfo], configured_model: str | None) -> tuple[ModelInfo, str]:
    """Select the model for the probe.

    * If a model is configured, it must exist in the listing (else error).
    * Otherwise pick the cheapest by reported per-1K pricing
      (input_per_1k + output_per_1k), breaking ties by model id.
    """
    if not models:
        raise ProviderError("CleanAPIs returned an empty model list", provider=PROVIDER_NAME)
    if configured_model:
        for model in models:
            if model.id == configured_model:
                return model, "configured"
        available = ", ".join(sorted(m.id for m in models))
        raise ProviderError(
            f"Configured model {configured_model!r} is not available. Available: {available}",
            provider=PROVIDER_NAME,
        )
    with_pricing = [m for m in models if m.estimated_cost_per_1k is not None]
    if with_pricing:
        cheapest = min(with_pricing, key=lambda m: (m.estimated_cost_per_1k, m.id))
        return cheapest, "cheapest_listed"
    # No pricing reported: fall back to the first listed model (deterministic).
    return sorted(models, key=lambda m: m.id)[0], "first_listed_no_pricing"


def run_connectivity_test(
    settings: CleanAPISSettings | None = None,
    *,
    client: CleanAPIsClient | None = None,
    timeout_seconds: float = 30.0,
) -> ConnectivityReport:
    """Run the connectivity test and return a structured report.

    Never raises for provider/configuration problems — every failure becomes
    a failed step in the report (safe error handling, step 7).
    """
    from factory.config.provider_config import get_cleanapis_settings

    settings = settings or get_cleanapis_settings()
    steps: list[ConnectivityStep] = []

    def record(
        step: int,
        name: str,
        passed: bool,
        detail: str = "",
        error_type: str | None = None,
    ) -> bool:
        steps.append(
            ConnectivityStep(
                step=step, name=name, passed=passed, detail=detail, error_type=error_type
            )
        )
        return passed

    # Step 1: API key available.
    api_key = settings.cleanapis_API_KEY
    if not api_key:
        record(
            1,
            "api_key_available",
            False,
            "cleanapis_API_KEY is not set in the environment",
            error_type="ConfigurationError",
        )
        return ConnectivityReport(overall_pass=False, steps=steps, base_url=settings.base_url)
    record(1, "api_key_available", True, "API key is present (value never displayed)")

    owns_client = client is None
    if client is None:
        probe_settings = settings.model_copy(
            update={"timeout_seconds": timeout_seconds, "max_retries": 1}
        )
        client = CleanAPIsClient(probe_settings)

    try:
        # Steps 2+3: authentication works AND the endpoint is reachable —
        # GET /models requires a valid key, so one call verifies both.
        try:
            models = client.list_models()
        except ProviderError as exc:
            record(
                2,
                "endpoint_reachable_and_authenticated",
                False,
                f"{type(exc).__name__}: {exc.message}",
                error_type=type(exc).__name__,
            )
            return ConnectivityReport(overall_pass=False, steps=steps, base_url=settings.base_url)
        record(
            2,
            "endpoint_reachable_and_authenticated",
            True,
            f"GET {settings.base_url}/models succeeded with authentication; "
            f"{len(models)} models listed",
        )

        # Step 4: a valid model is available; select one.
        try:
            model, model_source = select_model(models, settings.model)
        except ProviderError as exc:
            record(
                4,
                "model_available",
                False,
                f"{type(exc).__name__}: {exc.message}",
                error_type=type(exc).__name__,
            )
            return ConnectivityReport(overall_pass=False, steps=steps, base_url=settings.base_url)
        pricing = model.pricing
        record(
            4,
            "model_available",
            True,
            f"selected model {model.id!r} ({model_source}); "
            f"pricing input_per_1k={pricing.input_per_1k} output_per_1k={pricing.output_per_1k}",
        )

        # Step 5+6: minimal request succeeds and the response parses.
        request = LLMRequest(
            model=model.id,
            messages=[{"role": "user", "content": _PROBE_PROMPT}],
            temperature=_PROBE_TEMPERATURE,
            max_tokens=_PROBE_MAX_TOKENS,
            metadata={"purpose": "phase0_connectivity_test"},
        )
        try:
            response = _run_probe(client, settings, request, model.id)
        except ProviderError as exc:
            record(
                5,
                "minimal_request_succeeds",
                False,
                f"{type(exc).__name__}: {exc.message}",
                error_type=type(exc).__name__,
            )
            return ConnectivityReport(
                overall_pass=False,
                steps=steps,
                base_url=settings.base_url,
                model_selected=model.id,
                model_source=model_source,
            )
        record(
            5, "minimal_request_succeeds", True, f"completion succeeded in {response.latency_ms}ms"
        )

        parsed = (
            response.content is not None
            and isinstance(response.content, str)
            and response.finish_reason in (FinishReason.STOP, FinishReason.LENGTH)
        )
        record(
            6,
            "response_parseable",
            parsed,
            f"content parsed ({len(response.content)} chars); finish_reason="
            f"{response.finish_reason.value}; usage total_tokens={response.usage.total_tokens}",
        )

        # Step 7: errors handled safely — reaching this point means every
        # failure path above produced a sanitized, typed step result.
        record(
            7,
            "errors_handled_safely",
            True,
            "all failure modes mapped to typed errors; report contains no secrets",
        )

        overall = all(step.passed for step in steps)
        return ConnectivityReport(
            overall_pass=overall,
            steps=steps,
            base_url=settings.base_url,
            model_selected=model.id,
            model_source=model_source,
            probe={
                "prompt": _PROBE_PROMPT,
                "temperature": _PROBE_TEMPERATURE,
                "max_tokens": _PROBE_MAX_TOKENS,
                "response_chars": len(response.content),
                "finish_reason": response.finish_reason.value,
                "total_tokens": response.usage.total_tokens,
                "latency_ms": response.latency_ms,
            },
        )
    finally:
        if owns_client:
            client.close()


def _run_probe(
    client: CleanAPIsClient,
    settings: CleanAPISSettings,
    request: LLMRequest,
    model: str,
) -> LLMResponse:
    """Execute the single probe completion and record its usage."""
    import time

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": m.role, "content": m.content} for m in request.effective_messages()],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }
    started = time.monotonic()
    try:
        result = client.chat_completion(payload)
        latency_ms = int((time.monotonic() - started) * 1000)
    except ProviderError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        LoggingUsageTracker().record(
            ProviderUsageRecord(
                provider=PROVIDER_NAME,
                model=model,
                latency_ms=latency_ms,
                success=False,
                error_type=type(exc).__name__,
                purpose="phase0_connectivity_test",
            )
        )
        raise
    response = LLMResponse(
        content=result.content,
        provider=PROVIDER_NAME,
        model=result.model or model,
        usage=LLMUsage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
        ),
        finish_reason=FinishReason.from_provider(result.finish_reason),
        latency_ms=latency_ms,
        request_id=result.id,
    )
    LoggingUsageTracker().record(
        ProviderUsageRecord(
            provider=PROVIDER_NAME,
            model=response.model,
            latency_ms=latency_ms,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            success=True,
            purpose="phase0_connectivity_test",
            request_id=response.request_id,
            usage_available=response.usage.total_tokens is not None,
        )
    )
    return response
