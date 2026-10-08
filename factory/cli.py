"""Command-line interface.

Commands:

* ``factory health`` — local health check (config, database, storage, provider).
* ``factory db upgrade|check`` — run/verify Alembic migrations.
* ``factory cleanapis test-connection`` — the REAL CleanAPIs connectivity test.
* ``factory serve`` — run the HTTP API (health check) with uvicorn.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from factory import __version__
from factory.config.settings import sanitize_database_url
from factory.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _cmd_health(args: argparse.Namespace) -> int:
    from factory.config.provider_config import get_cleanapis_settings
    from factory.config.settings import get_app_settings
    from factory.storage.db import create_engine_from_url, make_session_factory

    settings = get_app_settings()
    cleanapis = get_cleanapis_settings()

    report: dict[str, Any] = {
        "service": settings.app_name,
        "version": __version__,
        "environment": settings.environment.value,
        "checks": {},
    }

    # Database (reachability + schema status; never creates or migrates schema —
    # that is `make migrate`'s job, so health and migrations never conflict).
    try:
        from sqlalchemy import text

        from factory.storage.db import schema_is_initialized

        engine = create_engine_from_url(settings.database_url)
        session_factory = make_session_factory(engine)

        with session_factory() as session:
            session.execute(text("SELECT 1"))
            initialized = schema_is_initialized(session)
        report["checks"]["database"] = {
            "status": "ok",
            "url": sanitize_database_url(settings.database_url),
            "schema_initialized": initialized,
            "detail": None if initialized else "run `make migrate` to initialize the schema",
        }
    except Exception as exc:  # noqa: BLE001 - health check must not raise
        report["checks"]["database"] = {"status": "error", "detail": type(exc).__name__}

    # Artifact storage.
    try:
        root = Path(settings.artifact_storage_dir)
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report["checks"]["artifact_storage"] = {"status": "ok", "path": str(root)}
    except Exception as exc:  # noqa: BLE001 - health check must not raise
        report["checks"]["artifact_storage"] = {"status": "error", "detail": type(exc).__name__}

    # CleanAPIs configuration (never the key).
    report["checks"]["cleanapis"] = {
        "configured": cleanapis.is_configured,
        "base_url": cleanapis.base_url,
        "model_configured": bool(cleanapis.model),
    }

    ok = all(
        report["checks"][name].get("status") == "ok" for name in ("database", "artifact_storage")
    )
    report["status"] = "ok" if ok else "degraded"
    print(json.dumps(report, indent=2, default=str))
    return 0 if ok else 1


def _cmd_db(args: argparse.Namespace) -> int:
    from alembic.config import Config

    from alembic import command
    from factory.config.settings import get_app_settings

    settings = get_app_settings()
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    if args.db_command == "upgrade":
        command.upgrade(cfg, "head")
        print(f"Database migrated to head ({sanitize_database_url(settings.database_url)})")
        return 0
    if args.db_command == "check":
        command.check(cfg)
        print("No new upgrade operations detected.")
        return 0
    if args.db_command == "heads":
        command.heads(cfg)
        return 0
    print(f"Unknown db command: {args.db_command}", file=sys.stderr)
    return 2


def _cmd_cleanapis_test_connection(args: argparse.Namespace) -> int:
    from factory.config.provider_config import get_cleanapis_settings
    from factory.providers.cleanapis.connectivity import run_connectivity_test

    settings = get_cleanapis_settings()
    report = run_connectivity_test(settings, timeout_seconds=args.timeout)
    print(json.dumps(report.to_dict(), indent=2, default=str))
    if report.overall_pass:
        print("CleanAPIs connectivity test: PASS", file=sys.stderr)
        return 0
    print("CleanAPIs connectivity test: FAIL", file=sys.stderr)
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from factory.api.main import create_app
    from factory.config.settings import get_app_settings
    from factory.storage.db import create_engine_from_url, make_session_factory

    settings = get_app_settings()
    engine = create_engine_from_url(settings.database_url)
    session_factory = make_session_factory(engine)
    app = create_app(settings=settings, session_factory=session_factory)
    uvicorn.run(app, host=args.host or settings.api_host, port=args.port or settings.api_port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description=__doc__)
    parser.add_argument("--version", action="version", version=f"factory {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="Run a local health check").set_defaults(func=_cmd_health)

    db_parser = subparsers.add_parser("db", help="Database migrations (Alembic)")
    db_parser.add_argument("db_command", choices=["upgrade", "check", "heads"])
    db_parser.set_defaults(func=_cmd_db)

    cleanapis_parser = subparsers.add_parser("cleanapis", help="CleanAPIs utilities")
    cleanapis_sub = cleanapis_parser.add_subparsers(dest="cleanapis_command", required=True)
    test_conn = cleanapis_sub.add_parser(
        "test-connection", help="Run the real CleanAPIs connectivity test"
    )
    test_conn.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds"
    )
    test_conn.set_defaults(func=_cmd_cleanapis_test_connection)

    serve_parser = subparsers.add_parser("serve", help="Run the HTTP API server")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()
    try:
        return int(args.func(args))
    except Exception as exc:
        logger.exception("command_failed")
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
