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

    # YouTube Data API configuration (never the key).
    from factory.config.youtube_config import get_youtube_settings

    youtube = get_youtube_settings()
    report["checks"]["youtube"] = {
        "configured": youtube.is_configured,
        "base_url": youtube.base_url,
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


def _build_pipeline() -> tuple[Any, Any]:
    """Build the production intelligence pipeline (DB-backed wiring)."""
    from factory.config.settings import get_app_settings, register_runtime_secrets
    from factory.intelligence.pipeline import build_default_pipeline
    from factory.storage.artifacts import ArtifactStore
    from factory.storage.db import create_engine_from_url, make_session_factory

    register_runtime_secrets()
    settings = get_app_settings()
    engine = create_engine_from_url(settings.database_url)
    session_factory = make_session_factory(engine)
    artifact_store = ArtifactStore(session_factory, settings.artifact_storage_dir)
    return build_default_pipeline(
        session_factory=session_factory, artifact_store=artifact_store
    ), session_factory


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    import json as _json

    from factory.jobs.service import JobService
    from factory.jobs.types import JobType

    pipeline, session_factory = _build_pipeline()
    service = JobService(session_factory)
    payload = _json.loads(args.payload) if args.payload else {}
    job_type = JobType(args.job_type)
    job = service.enqueue(
        job_type,
        project_id=args.project_id,
        payload=payload,
        idempotency_key=args.idempotency_key,
        input_artifact_id=args.input_artifact_id,
    )
    record = pipeline.run_job(service, job.id)
    print(
        _json.dumps(
            {
                "job_id": record.id,
                "type": record.type.value,
                "status": record.status.value,
                "output_artifact_id": record.output_artifact_id,
                "error": record.error,
            },
            indent=2,
            default=str,
        )
    )
    return 0 if record.status.value == "succeeded" else 1


def _cmd_pipeline_run_chain(args: argparse.Namespace) -> int:
    """Run discovery → market analysis → opportunity detection in sequence."""
    import json as _json

    from factory.jobs.service import JobService
    from factory.jobs.types import JobType

    pipeline, session_factory = _build_pipeline()
    service = JobService(session_factory)
    payload = _json.loads(args.payload) if args.payload else {}

    discovery = service.enqueue(
        JobType.YOUTUBE_DISCOVERY, project_id=args.project_id, payload=payload
    )
    record = pipeline.run_job(service, discovery.id)
    if record.status.value != "succeeded":
        print(
            _json.dumps(
                {"stage": "discovery", "status": record.status.value, "error": record.error},
                indent=2,
                default=str,
            )
        )
        return 1

    analysis = service.enqueue(
        JobType.MARKET_ANALYSIS,
        project_id=args.project_id,
        input_artifact_id=record.output_artifact_id,
    )
    record = pipeline.run_job(service, analysis.id)
    if record.status.value != "succeeded":
        print(
            _json.dumps(
                {"stage": "analysis", "status": record.status.value, "error": record.error},
                indent=2,
                default=str,
            )
        )
        return 1

    opportunities = service.enqueue(
        JobType.OPPORTUNITY_DETECTION,
        project_id=args.project_id,
        input_artifact_id=record.output_artifact_id,
    )
    record = pipeline.run_job(service, opportunities.id)
    print(
        _json.dumps(
            {
                "discovery_artifact_id": discovery.output_artifact_id,
                "analysis_artifact_id": analysis.output_artifact_id,
                "opportunity_list_artifact_id": record.output_artifact_id,
                "status": record.status.value,
                "error": record.error,
            },
            indent=2,
            default=str,
        )
    )
    return 0 if record.status.value == "succeeded" else 1


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

    pipeline_parser = subparsers.add_parser("pipeline", help="Intelligence pipeline (Phase 1)")
    pipeline_sub = pipeline_parser.add_subparsers(dest="pipeline_command", required=True)
    run_parser = pipeline_sub.add_parser("run", help="Enqueue and run a single pipeline job")
    run_parser.add_argument(
        "--job-type",
        required=True,
        choices=["youtube_discovery", "market_analysis", "opportunity_detection"],
    )
    run_parser.add_argument("--project-id", required=True)
    run_parser.add_argument("--payload", default=None, help="JSON job payload")
    run_parser.add_argument("--idempotency-key", default=None)
    run_parser.add_argument("--input-artifact-id", default=None)
    run_parser.set_defaults(func=_cmd_pipeline_run)
    chain_parser = pipeline_sub.add_parser(
        "run-chain", help="Run discovery → analysis → opportunities in sequence"
    )
    chain_parser.add_argument("--project-id", required=True)
    chain_parser.add_argument("--payload", default=None, help="JSON discovery payload")
    chain_parser.set_defaults(func=_cmd_pipeline_run_chain)

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
