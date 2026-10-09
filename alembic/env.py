"""Alembic environment — wired to the factory ORM metadata and app settings."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from factory.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Database URL: explicit env override, else application settings."""
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    from factory.config.settings import get_app_settings

    return get_app_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live database)."""
    configuration = config.get_section(config.config_ini_section, {})
    url = _database_url()
    configuration["sqlalchemy.url"] = url
    # Ensure the parent directory exists for file-based SQLite databases so a
    # fresh checkout can migrate without manual setup.
    if url.startswith("sqlite"):
        from factory.storage.db import _sqlite_file_path

        db_path = _sqlite_file_path(url)
        if db_path is not None:
            db_path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
