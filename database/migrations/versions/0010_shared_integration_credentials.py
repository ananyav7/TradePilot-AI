"""Persist encrypted OAuth access token for the isolated worker."""
from alembic import op
import sqlalchemy as sa

revision = "0010_shared_integration_credentials"
down_revision = "0009_scheduler_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "integration_credentials" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "integration_credentials",
        sa.Column("provider", sa.String(length=40), primary_key=True),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("integration_credentials")
