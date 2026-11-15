"""Database initialization command: ``python -m database.init_db``."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from config.settings import PROJECT_ROOT


def initialize_database() -> None:
    """Apply all pending Alembic migrations to the configured database."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")


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
