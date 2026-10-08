"""REAL CleanAPIs connectivity test (live).

This is the ONLY test that talks to the real CleanAPIs API. It is:

* marked ``live`` (excluded from the default test run via ``-m "not live"``);
* skipped unless ``cleanapis_API_KEY`` (or ``CLEANAPIS_API_KEY``) is set;
* minimal: one GET /models plus one tiny deterministic completion;
* never prints or stores the API key.

Run it explicitly with:

    pytest -m live tests/integration -v
    # or
    python -m factory.cli cleanapis test-connection
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.live

KEY_PRESENT = bool(
    os.environ.get("cleanapis_API_KEY")  # noqa: SIM112 - exact documented env var name
    or os.environ.get("CLEANAPIS_API_KEY")
)


@pytest.mark.skipif(not KEY_PRESENT, reason="cleanapis_API_KEY is not set; live test skipped")
def test_cleanapis_live_connectivity() -> None:
    from factory.providers.cleanapis.connectivity import run_connectivity_test

    report = run_connectivity_test()
    print(json.dumps(report.to_dict(), indent=2))
    assert report.overall_pass, "CleanAPIs connectivity test failed:\n" + json.dumps(
        report.to_dict(), indent=2
    )
    step_names = [step.name for step in report.steps]
    assert step_names == [
        "api_key_available",
        "endpoint_reachable_and_authenticated",
        "model_available",
        "minimal_request_succeeds",
        "response_parseable",
        "errors_handled_safely",
    ]
    assert report.model_selected
    # The probe response content is not stored, but usage is reported.
    assert report.probe["finish_reason"] in ("stop", "length")
