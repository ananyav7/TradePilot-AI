"""Copy an existing SQLite database into an already-migrated PostgreSQL database.

The destination must be empty (apart from Alembic metadata). The script never
truncates or overwrites data. Back up both databases before running it.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text

from database.connection import engine as target_engine
from database.models import Base


def migrate(source_path: Path) -> int:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")
    if target_engine.dialect.name != "postgresql":
        raise RuntimeError("DATABASE_URL must point to PostgreSQL")
    source_engine = create_engine(f"sqlite:///{source_path.resolve().as_posix()}")
    copied = 0
    try:
        source_tables = set(inspect(source_engine).get_table_names())
        target_tables = set(inspect(target_engine).get_table_names())
        expected = {table.name for table in Base.metadata.sorted_tables}
        if not expected.issubset(target_tables):
            raise RuntimeError("Target schema is incomplete; run Alembic migrations first")
        with target_engine.begin() as target:
            for table in Base.metadata.sorted_tables:
                if table.name not in source_tables:
                    continue
                if table.name == "alembic_version":
                    continue
                count = target.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar_one()
                if count:
                    raise RuntimeError(f"Target table {table.name} is not empty; refusing to duplicate records")
            with source_engine.connect() as source:
                for table in Base.metadata.sorted_tables:
                    if table.name not in source_tables or table.name == "alembic_version":
                        continue
                    result = source.execute(select(table))
                    while batch := result.mappings().fetchmany(1000):
                        target.execute(table.insert(), [dict(row) for row in batch])
                        copied += len(batch)
                    if target_engine.dialect.name == "postgresql":
                        for column in table.primary_key.columns:
                            if column.autoincrement is True or column.autoincrement == "auto":
                                quoted_table = target.dialect.identifier_preparer.quote(table.name)
                                quoted_column = target.dialect.identifier_preparer.quote(column.name)
                                maximum = target.execute(text(f"SELECT MAX({quoted_column}) FROM {quoted_table}")).scalar_one()
                                if maximum is not None:
                                    target.execute(text("SELECT setval(pg_get_serial_sequence(:table_name, :column_name), :maximum, true)"),
                                                   {"table_name": table.name, "column_name": column.name, "maximum": maximum})
        return copied
    finally:
        source_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="path to the existing SQLite database file")
    args = parser.parse_args()
    print(f"Copied {migrate(args.source)} records")
