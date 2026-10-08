"""Health check API tests (basic health check deliverable)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from factory.api.main import create_app
from factory.config.provider_config import CleanAPISSettings
from factory.storage.db import create_engine_from_url, init_db, make_session_factory

SECRET = "cc_health_api_secret_424242"


def _client(tmp_path):
    engine = create_engine_from_url(f"sqlite:///{tmp_path}/health.db")
    init_db(engine)
    session_factory = make_session_factory(engine)
    from factory.config.settings import AppSettings

    settings = AppSettings(
        database_url=f"sqlite:///{tmp_path}/health.db",
        artifact_storage_dir=str(tmp_path / "artifacts"),
    )
    cleanapis = CleanAPISSettings(cleanapis_API_KEY=SECRET, model="test-model")
    app = create_app(
        settings=settings, cleanapis_settings=cleanapis, session_factory=session_factory
    )
    return TestClient(app)


def test_health_ok(tmp_path):
    client = _client(tmp_path)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["artifact_storage"]["status"] == "ok"
    assert body["checks"]["cleanapis"]["configured"] is True
    assert body["checks"]["cleanapis"]["base_url"] == "https://cleanapis.com/v1"
    # The database URL is sanitized (no credentials possible for sqlite, but check shape).
    assert "sqlite" in body["checks"]["database"]["url"]


def test_health_never_exposes_secrets(tmp_path):
    client = _client(tmp_path)
    body = client.get("/health").json()
    assert SECRET not in str(body)
    assert "cc_" not in str(body)


def test_root(tmp_path):
    client = _client(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "ai-youtube-autonomous-factory"


def test_health_degraded_when_database_broken(tmp_path):
    from factory.config.settings import AppSettings

    settings = AppSettings(
        database_url=f"sqlite:///{tmp_path}/health.db",
        artifact_storage_dir=str(tmp_path / "artifacts"),
    )

    def broken_session_factory():
        raise RuntimeError("database is down")

    app = create_app(
        settings=settings,
        cleanapis_settings=CleanAPISSettings(cleanapis_API_KEY=None),
        session_factory=broken_session_factory,
    )
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "error"
    assert body["checks"]["cleanapis"]["configured"] is False
