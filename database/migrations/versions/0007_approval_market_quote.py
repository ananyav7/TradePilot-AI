"""Audit the exact sourced market quote used for manual approval."""

from alembic import op
import sqlalchemy as sa

revision = "0007_approval_market_quote"
down_revision = "0006_manual_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("market_data_source", sa.String(120), nullable=True))
        batch.add_column(sa.Column("market_data_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("market_data_at")
        batch.drop_column("market_data_source")
