"""Add paper order position links and persistent position protection levels."""

from alembic import op
import sqlalchemy as sa

revision = "0005_paper_trading"
down_revision = "0004_trailing_high_watermark"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("paper_orders") as batch:
        batch.alter_column("approval_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("position_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("stock_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("stop_loss", sa.Numeric(16, 4), nullable=True))
        batch.add_column(sa.Column("target_price", sa.Numeric(16, 4), nullable=True))
        batch.add_column(sa.Column("reason", sa.Text(), nullable=True))
        batch.create_foreign_key("fk_paper_orders_position_id", "positions", ["position_id"], ["id"],
                                 ondelete="SET NULL")
        batch.create_index("ix_paper_orders_position_id", ["position_id"], unique=False)
        batch.create_foreign_key("fk_paper_orders_stock_id", "stocks", ["stock_id"], ["id"], ondelete="RESTRICT")
        batch.create_index("ix_paper_orders_stock_id", ["stock_id"], unique=False)
        batch.alter_column("status", existing_type=sa.String(length=24), nullable=False,
                           server_default="PENDING")
    with op.batch_alter_table("positions") as batch:
        batch.add_column(sa.Column("stop_loss", sa.Numeric(16, 4), nullable=True))
        batch.add_column(sa.Column("target_price", sa.Numeric(16, 4), nullable=True))
    connection = op.get_bind()
    connection.execute(sa.text("""UPDATE paper_orders SET stock_id = (
        SELECT trade_signals.stock_id FROM approvals
        JOIN trade_signals ON trade_signals.id = approvals.signal_id
        WHERE approvals.id = paper_orders.approval_id
    ) WHERE stock_id IS NULL"""))
    connection.execute(sa.text("UPDATE paper_orders SET status = 'PENDING' WHERE lower(status) IN ('created', 'pending')"))
    connection.execute(sa.text("UPDATE paper_orders SET status = 'APPROVED' WHERE lower(status) = 'approved'"))
    connection.execute(sa.text("UPDATE paper_orders SET status = 'FILLED' WHERE lower(status) = 'filled'"))
    connection.execute(sa.text("UPDATE paper_orders SET status = 'PARTIALLY_FILLED' WHERE lower(status) IN ('partial', 'partially_filled')"))
    connection.execute(sa.text("UPDATE paper_orders SET status = upper(status) WHERE status NOT IN ('PENDING', 'APPROVED', 'FILLED', 'PARTIALLY_FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED')"))


def downgrade() -> None:
    with op.batch_alter_table("positions") as batch:
        batch.drop_column("target_price")
        batch.drop_column("stop_loss")
    with op.batch_alter_table("paper_orders") as batch:
        batch.drop_index("ix_paper_orders_position_id")
        batch.drop_index("ix_paper_orders_stock_id")
        batch.drop_constraint("fk_paper_orders_position_id", type_="foreignkey")
        batch.drop_constraint("fk_paper_orders_stock_id", type_="foreignkey")
        batch.drop_column("position_id")
        batch.drop_column("stock_id")
        batch.drop_column("reason")
        batch.drop_column("target_price")
        batch.drop_column("stop_loss")
