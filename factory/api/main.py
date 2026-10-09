"""FastAPI application — Phase 0 exposes a basic health check.

``GET /health`` reports service status without exposing secrets: the CleanAPIs
section reports only whether a key is configured (never the key), and the
database URL is sanitized.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from factory import __version__
from factory.config.provider_config import CleanAPISSettings
from factory.config.settings import AppSettings, sanitize_database_url
from factory.observability.logging import get_logger

logger = get_logger(__name__)


def create_app(
    settings: AppSettings | None = None,
    cleanapis_settings: CleanAPISSettings | None = None,
    session_factory: Any = None,
) -> FastAPI:
    """Application factory (dependency injection keeps this testable)."""
    from factory.config.provider_config import get_cleanapis_settings
    from factory.config.settings import get_app_settings

    settings = settings or get_app_settings()
    cleanapis_settings = cleanapis_settings or get_cleanapis_settings()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="Autonomous AI YouTube content intelligence and production platform",
    )

    @app.get("/")
    def root() -> dict[str, str]:
        return {"service": settings.app_name, "version": __version__}

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Basic health check: process, database, storage, provider config."""
        checks: dict[str, Any] = {}

        checks["database"] = _check_database(session_factory, settings)
        checks["artifact_storage"] = _check_storage(settings)
        checks["cleanapis"] = {
            "configured": cleanapis_settings.is_configured,
            "base_url": cleanapis_settings.base_url,
            "model_configured": bool(cleanapis_settings.model),
        }
        from factory.config.youtube_config import get_youtube_settings

        youtube_settings = get_youtube_settings()
        checks["youtube"] = {
            "configured": youtube_settings.is_configured,
            "base_url": youtube_settings.base_url,
        }

        ok = all(
            check.get("status") == "ok" if isinstance(check, dict) else bool(check)
            for check in checks.values()
        )
        # CleanAPIs key presence is informational, not a health failure.
        ok = (
            checks["database"].get("status") == "ok"
            and checks["artifact_storage"].get("status") == "ok"
        )
        return {
            "status": "ok" if ok else "degraded",
            "version": __version__,
            "environment": settings.environment.value,
            "checks": checks,
        }

    return app


def _check_database(session_factory: Any, settings: AppSettings) -> dict[str, Any]:
    if session_factory is None:
        return {"status": "unknown", "detail": "no session factory bound"}
    try:
        from factory.storage.db import schema_is_initialized

        with session_factory() as session:
            session.execute(text("SELECT 1"))
            initialized = schema_is_initialized(session)
        return {
            "status": "ok",
            "url": sanitize_database_url(settings.database_url),
            "schema_initialized": initialized,
        }
    except Exception as exc:
        logger.exception("health_check_database_failed")
        return {"status": "error", "detail": type(exc).__name__}


def _check_storage(settings: AppSettings) -> dict[str, Any]:
    try:
        root = Path(settings.artifact_storage_dir)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"status": "ok", "path": str(root)}
    except Exception as exc:
        logger.exception("health_check_storage_failed")
        return {"status": "error", "detail": type(exc).__name__}
