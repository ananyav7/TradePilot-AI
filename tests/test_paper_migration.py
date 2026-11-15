"""Verify paper schema migration preserves prior order/fill links on SQLite."""

import os
import sqlite3
import subprocess
import sys

from config.settings import PROJECT_ROOT


def test_paper_migration_preserves_existing_trade_history(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('0004_trailing_high_watermark');
        CREATE TABLE stocks(id INTEGER PRIMARY KEY);
        INSERT INTO stocks VALUES (1);
        CREATE TABLE positions(id INTEGER PRIMARY KEY);
        CREATE TABLE trade_signals(id INTEGER PRIMARY KEY, stock_id INTEGER);
        INSERT INTO trade_signals VALUES (1, 1);
        CREATE TABLE approvals(id INTEGER PRIMARY KEY, signal_id INTEGER);
        INSERT INTO approvals VALUES (1, 1);
        CREATE TABLE paper_orders(
            id INTEGER PRIMARY KEY, approval_id INTEGER NOT NULL UNIQUE REFERENCES approvals(id),
            side VARCHAR(8) NOT NULL, quantity INTEGER NOT NULL, order_type VARCHAR(16) NOT NULL,
            status VARCHAR(24) NOT NULL, filled_quantity INTEGER NOT NULL, average_price NUMERIC(16,4),
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        INSERT INTO paper_orders VALUES
            (1, 1, 'BUY', 2, 'market', 'created', 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        CREATE TABLE trade_history(
            id INTEGER PRIMARY KEY,
            paper_order_id INTEGER REFERENCES paper_orders(id) ON DELETE SET NULL
        );
        INSERT INTO trade_history VALUES (1, 1);
    """)
    connection.commit()
    connection.close()

    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(PROJECT_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=PROJECT_ROOT, env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    connection = sqlite3.connect(database_path)
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0009_scheduler_leases"
    approval_columns = {column[1] for column in connection.execute("PRAGMA table_info(approvals)")}
    assert {"signal_version", "approved_quantity", "risk_amount",
            "market_data_source", "market_data_at"} <= approval_columns
    assert connection.execute("SELECT status, stock_id FROM paper_orders WHERE id=1").fetchone() == ("PENDING", 1)
    assert connection.execute("SELECT paper_order_id FROM trade_history WHERE id=1").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()
