"""Migration tests: the Alembic migration set applies, downgrades, and reapplies."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

EXPECTED_TABLES = {
    "alembic_version",
    "artifacts",
    "assets",
    "channel_metrics",
    "channels",
    "content_briefs",
    "content_opportunities",
    "discovery_cache",
    "job_events",
    "llm_cache",
    "niches",
    "pipeline_jobs",
    "projects",
    "provider_usage",
    "providers",
    "research_claims",
    "research_documents",
    "sources",
    "topic_clusters",
    "topics",
    "video_metrics",
    "videos",
}


def _alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_upgrade_head_creates_full_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "migration.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(_alembic_config(url), "head")
    assert _tables(db_path) >= EXPECTED_TABLES


def test_downgrade_base_then_upgrade_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "migration.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert _tables(db_path) == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert _tables(db_path) >= EXPECTED_TABLES


def test_migration_matches_orm_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No drift: a fresh migrated DB has the same tables as ORM create_all."""
    from factory.storage.db import create_engine_from_url, init_db
    from factory.storage.models import Base

    db_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    command.upgrade(_alembic_config(f"sqlite:///{db_path}"), "head")

    orm_path = tmp_path / "orm.db"
    engine = create_engine_from_url(f"sqlite:///{orm_path}")
    init_db(engine)
    orm_tables = set(Base.metadata.tables.keys())
    migrated_tables = _tables(db_path) - {"alembic_version"}
    assert migrated_tables == orm_tables
