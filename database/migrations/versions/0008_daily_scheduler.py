"""Persist the reviewed NSE calendar and scheduler execution audit."""

from alembic import op
import sqlalchemy as sa

revision = "0008_daily_scheduler"
down_revision = "0007_approval_market_quote"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nse_calendar_years",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("source_name", sa.String(length=160), nullable=False),
        sa.Column("source_published_at", sa.DateTime(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "nse_calendar_exceptions",
        sa.Column("market_date", sa.Date(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("opens_at", sa.Time(), nullable=True),
        sa.Column("closes_at", sa.Time(), nullable=True),
        sa.ForeignKeyConstraint(["year"], ["nse_calendar_years.year"], ondelete="CASCADE"),
    )
    op.create_index("ix_nse_calendar_exceptions_year", "nse_calendar_exceptions", ["year"])
    op.create_table(
        "scheduler_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_key", sa.String(length=220), nullable=False, unique=True),
        sa.Column("job_name", sa.String(length=80), nullable=False),
        sa.Column("market_date", sa.Date(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("owner_id", sa.String(length=160), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("error_type", sa.String(length=120), nullable=True),
    )
    op.create_index("ix_scheduler_runs_name_date", "scheduler_runs", ["job_name", "market_date"])
    op.create_index("ix_scheduler_runs_status_started", "scheduler_runs", ["status", "started_at"])
    op.create_index("ix_scheduler_runs_job_name", "scheduler_runs", ["job_name"])
    op.create_index("ix_scheduler_runs_market_date", "scheduler_runs", ["market_date"])
    op.create_index("ix_scheduler_runs_status", "scheduler_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_scheduler_runs_status", table_name="scheduler_runs")
    op.drop_index("ix_scheduler_runs_market_date", table_name="scheduler_runs")
    op.drop_index("ix_scheduler_runs_job_name", table_name="scheduler_runs")
    op.drop_index("ix_scheduler_runs_status_started", table_name="scheduler_runs")
    op.drop_index("ix_scheduler_runs_name_date", table_name="scheduler_runs")
    op.drop_table("scheduler_runs")
    op.drop_index("ix_nse_calendar_exceptions_year", table_name="nse_calendar_exceptions")
    op.drop_table("nse_calendar_exceptions")
    op.drop_table("nse_calendar_years")
