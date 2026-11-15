"""Database initialization command: ``python -m database.init_db``."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from config.settings import PROJECT_ROOT
from database.connection import engine


def initialize_database() -> None:
    """Apply all pending Alembic migrations to the configured database."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    # SQLite DDL may implicitly end the transaction in some driver versions.
    # Stamp after successful upgrade so the revision state is durable as well.
    command.stamp(config, "head")
    head_revision = ScriptDirectory.from_config(config).get_current_head()
    # Ensure SQLite persists its version marker after DDL transaction boundaries.
    with engine.begin() as connection:
        inspector = inspect(connection)
        if inspector.has_table("alembic_version"):
            current = connection.execute(text("SELECT version_num FROM alembic_version")).first()
            if current is None:
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                                   {"revision": head_revision})


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or back up the TradePilot database")
    parser.add_argument("--backup", action="store_true", help="create a consistent SQLite backup")
    parser.add_argument("--destination", type=Path, help="optional backup destination path")
    args = parser.parse_args()
    if args.backup:
        from database.backup import backup_database
        print(backup_database(args.destination))
    else:
        initialize_database()
        print("Database is up to date.")


if __name__ == "__main__":
    main()
