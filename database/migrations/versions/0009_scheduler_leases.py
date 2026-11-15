"""Add an atomic SQLite lease to prevent cross-process pipeline overlap."""

from alembic import op
import sqlalchemy as sa

revision = "0009_scheduler_leases"
down_revision = "0008_daily_scheduler"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_leases",
        sa.Column("resource", sa.String(length=80), primary_key=True),
        sa.Column("owner_id", sa.String(length=160), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_scheduler_leases_lease_until", "scheduler_leases", ["lease_until"])


def downgrade() -> None:
    op.drop_index("ix_scheduler_leases_lease_until", table_name="scheduler_leases")
    op.drop_table("scheduler_leases")
