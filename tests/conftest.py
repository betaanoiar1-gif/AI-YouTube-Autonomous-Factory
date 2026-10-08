"""Shared test fixtures.

Unit tests never touch the network or the real CleanAPIs API: the HTTP layer
is replaced with ``httpx.MockTransport`` and the API key is a fake value.
The REAL CleanAPIs connection is exercised only by the clearly separated
integration test in ``tests/integration/`` (marked ``live``, skipped unless
``cleanapis_API_KEY`` is set).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory.config.provider_config import CleanAPISSettings
from factory.config.settings import AppSettings, reset_settings_caches
from factory.observability.usage import ProviderUsageRecord
from factory.providers.cache import InMemoryLLMCache
from factory.providers.cleanapis.client import CleanAPIsClient
from factory.providers.cleanapis.provider import CleanAPIsProvider
from factory.storage.db import create_engine_from_url, init_db, make_session_factory

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_API_KEY = "cc_test_fake_key_0123456789abcdef"
FAKE_MODEL = "test-model"


class ListUsageTracker:
    """Usage tracker that collects records in memory for assertions."""

    def __init__(self) -> None:
        self.records: list[ProviderUsageRecord] = []

    def record(self, record: ProviderUsageRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _reset_settings_cache(monkeypatch: pytest.MonkeyPatch):
    """Isolate settings caches and keep the real environment out of tests."""
    monkeypatch.delenv("cleanapis_API_KEY", raising=False)
    monkeypatch.delenv("CLEANAPIS_API_KEY", raising=False)
    reset_settings_caches()
    yield
    reset_settings_caches()


@pytest.fixture
def tmp_db(tmp_path: Path):
    """A fresh SQLite database (schema created from ORM metadata)."""
    url = f"sqlite:///{tmp_path}/test.db"
    engine = create_engine_from_url(url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    yield engine, session_factory
    engine.dispose()


@pytest.fixture
def session_factory(tmp_db):
    return tmp_db[1]


@pytest.fixture
def app_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        artifact_storage_dir=str(tmp_path / "artifacts"),
        artifact_schemas_dir=str(REPO_ROOT / "schemas" / "artifacts"),
    )


@pytest.fixture
def cleanapis_settings() -> CleanAPISSettings:
    """Provider settings with a FAKE key — never a real credential."""
    return CleanAPISSettings(
        cleanapis_API_KEY=FAKE_API_KEY,
        model=FAKE_MODEL,
        max_retries=2,
    )


@pytest.fixture
def usage_tracker() -> ListUsageTracker:
    return ListUsageTracker()


def make_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    settings: CleanAPISSettings,
) -> CleanAPIsClient:
    """Build a CleanAPIsClient whose HTTP layer is a mock transport."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        base_url=settings.base_url,
        timeout=httpx.Timeout(settings.timeout_seconds),
    )
    return CleanAPIsClient(settings, http_client=http_client, sleep=lambda _seconds: None)


@pytest.fixture
def mock_client_factory(cleanapis_settings: CleanAPISSettings):
    def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> CleanAPIsClient:
        return make_mock_client(handler, cleanapis_settings)

    return _factory


@pytest.fixture
def provider_factory(cleanapis_settings: CleanAPISSettings, usage_tracker: ListUsageTracker):
    """Build a CleanAPIsProvider over a mock transport."""

    def _factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        cache: Any = None,
        budget: Any = None,
        budget_enforcer: Any = None,
    ) -> tuple[CleanAPIsProvider, ListUsageTracker]:
        client = make_mock_client(handler, cleanapis_settings)
        provider = CleanAPIsProvider(
            cleanapis_settings,
            client=client,
            usage_tracker=usage_tracker,
            cache=cache if cache is not None else InMemoryLLMCache(),
            budget=budget,
            budget_enforcer=budget_enforcer,
        )
        return provider, usage_tracker

    return _factory


def chat_completion_response(
    content: str = "Hello!",
    *,
    model: str = FAKE_MODEL,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    total_tokens: int = 15,
    request_id: str = "chatcmpl-test-1",
    rate_limit_headers: bool = True,
) -> httpx.Response:
    """A response in the documented CleanAPIs chat completion shape."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if rate_limit_headers:
        headers.update(
            {
                "X-RateLimit-Limit": "300",
                "X-RateLimit-Remaining": "299",
                "X-RateLimit-Reset": "1787305980",
                "X-RateLimit-Limit-Tokens": "500000",
                "X-RateLimit-Remaining-Tokens": "499985",
            }
        )
    return httpx.Response(
        200,
        json={
            "id": request_id,
            "object": "chat.completion",
            "created": 1787305982,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        },
        headers=headers,
    )


def error_response(
    status_code: int, message: str, error_type: str, code: str | None = None
) -> httpx.Response:
    """A response in the documented CleanAPIs error envelope shape."""
    error: dict[str, Any] = {"message": message, "type": error_type}
    if code:
        error["code"] = code
    return httpx.Response(status_code, json={"error": error})


@pytest.fixture
def projects(session_factory):
    """Create real project rows (jobs reference projects via FK)."""
    from factory.storage.models import Project

    with session_factory() as session:
        for project_id in ("proj-1", "p1", "p2"):
            session.add(Project(id=project_id, name=f"Project {project_id}"))
        session.commit()
    return session_factory


@pytest.fixture
def job_service(projects):
    from factory.jobs.service import JobService

    return JobService(projects, retry_base_seconds=1, retry_max_seconds=5)


@pytest.fixture
def artifact_store(session_factory, tmp_path: Path):
    from factory.storage.artifacts import ArtifactStore

    return ArtifactStore(session_factory, tmp_path / "artifacts")
