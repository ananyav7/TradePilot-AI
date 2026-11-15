"""Persist the next trailing-stop high-watermark in strategy signal snapshots."""

from alembic import op
import sqlalchemy as sa

revision = "0004_trailing_high_watermark"
down_revision = "0003_swing_strategy_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("trade_signals")}
    if "next_high_watermark" not in columns:
        op.add_column("trade_signals", sa.Column("next_high_watermark", sa.Numeric(16, 4), nullable=True))


def downgrade() -> None:
    op.drop_column("trade_signals", "next_high_watermark")
