"""Persist the exact signal and risk snapshot reviewed by the user."""

from alembic import op
import sqlalchemy as sa

revision = "0006_manual_approval"
down_revision = "0005_paper_trading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("approvals")}
    with op.batch_alter_table("approvals") as batch:
        if "signal_version" not in existing:
            batch.add_column(sa.Column("signal_version", sa.String(160), nullable=True))
        if "approved_quantity" not in existing:
            batch.add_column(sa.Column("approved_quantity", sa.Integer(), nullable=True))
        if "decision_entry_price" not in existing:
            batch.add_column(sa.Column("decision_entry_price", sa.Numeric(16, 4), nullable=True))
        if "decision_stop_loss" not in existing:
            batch.add_column(sa.Column("decision_stop_loss", sa.Numeric(16, 4), nullable=True))
        if "decision_target_price" not in existing:
            batch.add_column(sa.Column("decision_target_price", sa.Numeric(16, 4), nullable=True))
        if "risk_amount" not in existing:
            batch.add_column(sa.Column("risk_amount", sa.Numeric(16, 4), nullable=True))
        if "estimated_charges" not in existing:
            batch.add_column(sa.Column("estimated_charges", sa.Numeric(16, 4), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("estimated_charges")
        batch.drop_column("risk_amount")
        batch.drop_column("decision_target_price")
        batch.drop_column("decision_stop_loss")
        batch.drop_column("decision_entry_price")
        batch.drop_column("approved_quantity")
        batch.drop_column("signal_version")
