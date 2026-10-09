"""Database engine/session tests, including SQLite file-path handling."""

from __future__ import annotations

from pathlib import Path

from factory.storage.db import (
    _sqlite_file_path,
    create_engine_from_url,
    make_session_factory,
    session_scope,
)


class TestSqliteFilePath:
    def test_relative_url(self):
        assert _sqlite_file_path("sqlite:///./storage/factory.db") == Path("./storage/factory.db")

    def test_absolute_url(self):
        # Pure string parsing — no file is created at this path.
        assert _sqlite_file_path("sqlite:////var/lib/factory/factory.db") == Path(
            "/var/lib/factory/factory.db"
        )

    def test_memory_url(self):
        assert _sqlite_file_path("sqlite:///:memory:") is None
        assert _sqlite_file_path("sqlite://") is None

    def test_query_string_stripped(self):
        assert _sqlite_file_path("sqlite:///./storage/factory.db?check_same_thread=false") == Path(
            "./storage/factory.db"
        )


class TestEngineAndSessions:
    def test_creates_parent_directory_for_relative_url(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        engine = create_engine_from_url("sqlite:///./data/app.db")
        assert (tmp_path / "data").is_dir()
        engine.dispose()

    def test_creates_parent_directory_for_absolute_url(self, tmp_path: Path):
        db_path = tmp_path / "nested" / "deep" / "app.db"
        engine = create_engine_from_url(f"sqlite:///{db_path}")
        assert db_path.parent.is_dir()
        engine.dispose()

    def test_memory_engine(self):
        engine = create_engine_from_url("sqlite:///:memory:")
        engine.dispose()

    def test_session_scope_commits_and_rolls_back(self, tmp_path: Path):
        from factory.storage.models import Niche

        engine = create_engine_from_url(f"sqlite:///{tmp_path}/test.db")
        from factory.storage.db import init_db

        init_db(engine)
        session_factory = make_session_factory(engine)

        with session_scope(session_factory) as session:
            session.add(Niche(id="n1", name="History"))
        with session_scope(session_factory) as session:
            assert session.get(Niche, "n1") is not None

        try:
            with session_scope(session_factory) as session:
                session.add(Niche(id="n2", name="Duplicate-Name-Test"))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with session_scope(session_factory) as session:
            assert session.get(Niche, "n2") is None
        engine.dispose()
