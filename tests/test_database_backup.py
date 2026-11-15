"""SQLite backup and recovery integration tests."""

import sqlite3
from types import SimpleNamespace

import pytest

from database import backup as backup_module
from database.backup import backup_database


def test_database_backup_can_be_opened_and_recovers_committed_rows(tmp_path, monkeypatch):
    source = tmp_path / "tradepilot.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE recovery_check (value TEXT NOT NULL)")
        connection.execute("INSERT INTO recovery_check VALUES (?)", ("committed",))
    monkeypatch.setattr(backup_module, "get_settings",
                        lambda: SimpleNamespace(database_url=f"sqlite:///{source.as_posix()}"))

    target = backup_database(tmp_path / "restored" / "backup.db")

    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM recovery_check").fetchone() == ("committed",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("database_url", ["sqlite://", "sqlite:///:memory:", "postgresql://user/db"])
def test_database_backup_rejects_memory_or_non_sqlite_sources(database_url, monkeypatch):
    monkeypatch.setattr(backup_module, "get_settings",
                        lambda: SimpleNamespace(database_url=database_url))
    with pytest.raises(ValueError, match="file-backed SQLite"):
        backup_database()


def test_database_backup_reports_missing_source(tmp_path, monkeypatch):
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(backup_module, "get_settings",
                        lambda: SimpleNamespace(database_url=f"sqlite:///{missing.as_posix()}"))
    with pytest.raises(FileNotFoundError, match="Database does not exist"):
        backup_database()
