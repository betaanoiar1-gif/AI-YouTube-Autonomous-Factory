"""Connectivity test behavior WITHOUT a key / network (offline-safe paths).

The REAL live connectivity test lives in tests/integration/ and is marked
``live``; these tests verify the test harness itself fails safely.
"""

from __future__ import annotations

import pytest

from factory.config.provider_config import CleanAPISSettings
from factory.errors import ProviderError
from factory.providers.cleanapis.client import ModelInfo, ModelPricing
from factory.providers.cleanapis.connectivity import run_connectivity_test, select_model


class TestMissingKey:
    def test_missing_key_fails_step_1_without_raising(self):
        settings = CleanAPISSettings(cleanapis_API_KEY=None)
        report = run_connectivity_test(settings)
        assert report.overall_pass is False
        assert len(report.steps) == 1
        step = report.steps[0]
        assert step.step == 1
        assert step.name == "api_key_available"
        assert step.passed is False
        assert step.error_type == "ConfigurationError"
        # Safe error handling: the report contains no secrets and no exception.
        assert "cc_" not in str(report.to_dict())


class TestSelectModel:
    def _models(self) -> list[ModelInfo]:
        return [
            ModelInfo(id="expensive", pricing=ModelPricing(input_per_1k=0.01, output_per_1k=0.02)),
            ModelInfo(id="cheap", pricing=ModelPricing(input_per_1k=0.0001, output_per_1k=0.0002)),
            ModelInfo(id="mid", pricing=ModelPricing(input_per_1k=0.001, output_per_1k=0.002)),
        ]

    def test_configured_model_selected_when_available(self):
        model, source = select_model(self._models(), "mid")
        assert model.id == "mid"
        assert source == "configured"

    def test_configured_model_missing_raises(self):
        with pytest.raises(ProviderError):
            select_model(self._models(), "nonexistent")

    def test_cheapest_selected_by_default(self):
        model, source = select_model(self._models(), None)
        assert model.id == "cheap"
        assert source == "cheapest_listed"

    def test_ties_broken_by_id(self):
        models = [
            ModelInfo(id="b-model", pricing=ModelPricing(input_per_1k=0.001, output_per_1k=0.001)),
            ModelInfo(id="a-model", pricing=ModelPricing(input_per_1k=0.001, output_per_1k=0.001)),
        ]
        model, _ = select_model(models, None)
        assert model.id == "a-model"

    def test_empty_list_raises(self):
        with pytest.raises(ProviderError):
            select_model([], None)

    def test_no_pricing_falls_back_to_first_listed(self):
        models = [ModelInfo(id="z-model"), ModelInfo(id="a-model")]
        model, source = select_model(models, None)
        assert model.id == "a-model"
        assert source == "first_listed_no_pricing"
