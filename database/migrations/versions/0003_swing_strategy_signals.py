"""Persist reproducible strategy signals and enforce one signal per stock candle."""

from alembic import op
import sqlalchemy as sa

revision = "0003_swing_strategy_signals"
down_revision = "0002_stock_universe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("trade_signals")}
    new_columns = (
        ("signal_key", sa.String(length=80)),
        ("candle_at", sa.DateTime()),
        ("signal_date", sa.Date()),
        ("expires_at", sa.DateTime()),
        ("stop_loss", sa.Numeric(16, 4)),
        ("target_price", sa.Numeric(16, 4)),
        ("quantity_placeholder", sa.Integer()),
        ("ema20", sa.Numeric(16, 6)),
        ("ema50", sa.Numeric(16, 6)),
        ("rsi14", sa.Numeric(10, 6)),
        ("atr14", sa.Numeric(16, 6)),
        ("relative_volume", sa.Numeric(16, 6)),
        ("risk_reward", sa.Numeric(10, 6)),
        ("next_trailing_stop", sa.Numeric(16, 4)),
        ("recommended_action", sa.String(length=12)),
        ("exit_reason", sa.String(length=32)),
        ("strategy_version", sa.String(length=32), "swing-v1"),
    )
    for definition in new_columns:
        name, column_type, *default = definition
        if name not in columns:
            if default:
                op.add_column("trade_signals", sa.Column(name, column_type, nullable=False, server_default=default[0]))
            else:
                op.add_column("trade_signals", sa.Column(name, column_type, nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("trade_signals")}
    unique_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("trade_signals")}
    if "uq_trade_signals_stock_candle" not in unique_constraints and "uq_trade_signals_stock_candle" not in indexes:
        op.create_index("uq_trade_signals_stock_candle", "trade_signals", ["stock_id", "candle_at"], unique=True)
    if "uq_trade_signals_signal_key" not in unique_constraints and "uq_trade_signals_signal_key" not in indexes:
        op.create_index("uq_trade_signals_signal_key", "trade_signals", ["signal_key"], unique=True)
    if "ix_trade_signals_symbol_date" not in indexes:
        op.create_index("ix_trade_signals_symbol_date", "trade_signals", ["stock_id", "signal_date"])


def downgrade() -> None:
    op.drop_index("ix_trade_signals_symbol_date", table_name="trade_signals")
    op.drop_index("uq_trade_signals_signal_key", table_name="trade_signals")
    op.drop_index("uq_trade_signals_stock_candle", table_name="trade_signals")
    for column in ("strategy_version", "exit_reason", "recommended_action", "next_trailing_stop", "risk_reward", "relative_volume",
                   "atr14", "rsi14", "ema50", "ema20", "quantity_placeholder", "target_price", "stop_loss",
                   "expires_at", "signal_date", "candle_at", "signal_key"):
        op.drop_column("trade_signals", column)
