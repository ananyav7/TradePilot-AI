"""Add NIFTY 50 membership and instrument mapping state to stocks."""

from alembic import op
import sqlalchemy as sa

revision = "0002_stock_universe"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("stocks")}
    if "instrument_available" not in columns:
        op.add_column("stocks", sa.Column("instrument_available", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "in_nifty50" not in columns:
        op.add_column("stocks", sa.Column("in_nifty50", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "membership_source" not in columns:
        op.add_column("stocks", sa.Column("membership_source", sa.String(length=255), nullable=True))
    if "membership_updated_at" not in columns:
        op.add_column("stocks", sa.Column("membership_updated_at", sa.DateTime(), nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("stocks")}
    if "ix_stocks_instrument_available" not in indexes:
        op.create_index("ix_stocks_instrument_available", "stocks", ["instrument_available"])
    if "ix_stocks_in_nifty50" not in indexes:
        op.create_index("ix_stocks_in_nifty50", "stocks", ["in_nifty50"])


def downgrade() -> None:
    op.drop_index("ix_stocks_in_nifty50", table_name="stocks")
    op.drop_index("ix_stocks_instrument_available", table_name="stocks")
    op.drop_column("stocks", "membership_updated_at")
    op.drop_column("stocks", "membership_source")
    op.drop_column("stocks", "in_nifty50")
    op.drop_column("stocks", "instrument_available")
