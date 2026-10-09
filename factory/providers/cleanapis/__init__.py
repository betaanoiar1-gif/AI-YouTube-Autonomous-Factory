"""CleanAPIs provider — the real LLM provider for this project (Phase 0).

Verified surface (see docs/cleanapis.md):

* Base URL ``https://cleanapis.com/v1``, OpenAI-compatible.
* Auth: ``Authorization: Bearer cc_...``.
* ``GET /models``, ``POST /chat/completions``.
"""

from factory.providers.cleanapis.client import CleanAPIsClient, ModelInfo
from factory.providers.cleanapis.connectivity import (
    ConnectivityReport,
    ConnectivityStep,
    run_connectivity_test,
)
from factory.providers.cleanapis.provider import CleanAPIsProvider

__all__ = [
    "CleanAPIsClient",
    "CleanAPIsProvider",
    "ConnectivityReport",
    "ConnectivityStep",
    "ModelInfo",
    "run_connectivity_test",
]
