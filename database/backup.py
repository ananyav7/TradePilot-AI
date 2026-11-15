"""Consistent SQLite online backups."""

from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import unquote

from config.settings import PROJECT_ROOT, get_settings


def _sqlite_file(database_url: str) -> Path:
    if not database_url.startswith("sqlite:") or database_url in {"sqlite://", "sqlite:///:memory:"} or ":memory:" in database_url:
        raise ValueError("Backups require a file-backed SQLite database")
    raw_path = database_url.removeprefix("sqlite:///")
    path = Path(unquote(raw_path))
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def backup_database(destination: Path | str | None = None) -> Path:
    """Create a consistent SQLite backup and return its final path."""
    source = _sqlite_file(get_settings().database_url)
    if not source.exists():
        raise FileNotFoundError(f"Database does not exist: {source}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(destination) if destination is not None else source.parent / "backups" / f"{source.stem}_{stamp}{source.suffix}"
    if not target.is_absolute():
        target = (PROJECT_ROOT / target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with closing(sqlite3.connect(source)) as source_connection:
            with closing(sqlite3.connect(temporary)) as backup_connection:
                source_connection.backup(backup_connection)
                backup_connection.commit()
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


if __name__ == "__main__":
    print(backup_database())
