"""Track liveness for the isolated scheduler worker."""
from alembic import op
import sqlalchemy as sa

revision = "0011_scheduler_worker_heartbeat"
down_revision = "0010_shared_integration_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "scheduler_worker_heartbeat" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "scheduler_worker_heartbeat",
        sa.Column("worker_id", sa.String(length=80), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("scheduler_worker_heartbeat")
