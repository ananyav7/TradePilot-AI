"""Alembic environment wired to application settings and model metadata."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from config.settings import get_settings
from database.connection import Base
from database import models  # noqa: F401

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section) or {},
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        sqlite = connection.dialect.name == "sqlite"
        if sqlite:
            # SQLite's table-rebuild migrations must temporarily disable FK checks
            # so child rows keep referencing the rebuilt table instead of being
            # nulled/deleted when Alembic swaps the parent table.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
        try:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                              transactional_ddl=sqlite)
            with context.begin_transaction():
                context.run_migrations()
            if sqlite:
                violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                connection.commit()
                if violations:
                    raise RuntimeError(f"SQLite migration produced foreign-key violations: {violations[:5]}")
        finally:
            if sqlite:
                if connection.in_transaction():
                    connection.rollback()
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
