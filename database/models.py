"""SQLAlchemy domain models for local market research and paper trading."""

from datetime import date, datetime, time as datetime_time, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, ForeignKey, Index, Integer, Numeric, String, Time, event, inspect,
    Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.types import DateTime, TypeDecorator

from database.connection import Base


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC datetimes consistently and return timezone-aware UTC values."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Datetime values must include a timezone")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def utc_now() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False)


class Stock(TimestampMixin, Base):
    __tablename__ = "stocks"
    __table_args__ = (UniqueConstraint("exchange", "symbol", name="uq_stocks_exchange_symbol"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), default="NSE", nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    instrument_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    in_nifty50: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    membership_source: Mapped[str | None] = mapped_column(String(255))
    membership_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    market_data: Mapped[list["MarketData"]] = relationship(back_populates="stock", cascade="all, delete-orphan")
    indicators: Mapped[list["Indicators"]] = relationship(back_populates="stock", cascade="all, delete-orphan")
    signals: Mapped[list["TradeSignals"]] = relationship(back_populates="stock")
    positions: Mapped[list["Positions"]] = relationship(back_populates="stock")


class MarketData(TimestampMixin, Base):
    __tablename__ = "market_data"
    __table_args__ = (
        UniqueConstraint("stock_id", "interval", "candle_at", name="uq_market_data_candle"),
        CheckConstraint("open >= 0 AND high >= 0 AND low >= 0 AND close >= 0", name="ck_market_data_prices_nonnegative"),
        CheckConstraint("volume >= 0", name="ck_market_data_volume_nonnegative"),
        Index("ix_market_data_stock_market_date", "stock_id", "market_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False)
    interval: Mapped[str] = mapped_column(String(12), nullable=False)
    market_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    candle_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    stock: Mapped[Stock] = relationship(back_populates="market_data")


class Indicators(TimestampMixin, Base):
    __tablename__ = "indicators"
    __table_args__ = (UniqueConstraint("stock_id", "interval", "candle_at", "name", name="uq_indicators_observation"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False)
    interval: Mapped[str] = mapped_column(String(12), nullable=False)
    market_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    candle_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stock: Mapped[Stock] = relationship(back_populates="indicators")


class TradeSignals(TimestampMixin, Base):
    __tablename__ = "trade_signals"
    __table_args__ = (
        Index("uq_trade_signals_stock_candle", "stock_id", "candle_at", unique=True),
        Index("uq_trade_signals_signal_key", "signal_key", unique=True),
        Index("ix_trade_signals_status_created", "status", "created_at"),
        Index("ix_trade_signals_symbol_date", "stock_id", "signal_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=False, index=True)
    signal_key: Mapped[str | None] = mapped_column(String(80))
    candle_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    signal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    rationale: Mapped[str | None] = mapped_column(Text)
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    quantity_placeholder: Mapped[int | None] = mapped_column(Integer)
    ema20: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    ema50: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    rsi14: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    atr14: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    relative_volume: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    risk_reward: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    next_trailing_stop: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    next_high_watermark: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    recommended_action: Mapped[str | None] = mapped_column(String(12))
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="swing-v1")
    stock: Mapped[Stock] = relationship(back_populates="signals")
    approval: Mapped["Approvals | None"] = relationship(back_populates="signal", uselist=False, cascade="all, delete-orphan")


class Approvals(TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("signal_id", name="uq_approvals_signal"), Index("ix_approvals_status_created", "status", "created_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("trade_signals.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    decision_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by: Mapped[str | None] = mapped_column(String(80))
    comment: Mapped[str | None] = mapped_column(Text)
    signal_version: Mapped[str | None] = mapped_column(String(160))
    approved_quantity: Mapped[int | None] = mapped_column(Integer)
    decision_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    decision_stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    decision_target_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    estimated_charges: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    market_data_source: Mapped[str | None] = mapped_column(String(120))
    market_data_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    signal: Mapped[TradeSignals] = relationship(back_populates="approval")
    paper_order: Mapped["PaperOrders | None"] = relationship(back_populates="approval", uselist=False)


class PaperOrders(TimestampMixin, Base):
    __tablename__ = "paper_orders"
    __table_args__ = (UniqueConstraint("approval_id", name="uq_paper_orders_approval"), Index("ix_paper_orders_status_created", "status", "created_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id", ondelete="RESTRICT"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), nullable=True, index=True)
    stock_id: Mapped[int | None] = mapped_column(ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=True, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="market")
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING", index=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    approval: Mapped[Approvals | None] = relationship(back_populates="paper_order")
    position: Mapped["Positions | None"] = relationship(back_populates="orders")
    stock: Mapped[Stock | None] = relationship()
    status_changes: Mapped[list["TradeStatusChanges"]] = relationship(back_populates="paper_order", cascade="all, delete-orphan", order_by="TradeStatusChanges.changed_at")
    history: Mapped[list["TradeHistory"]] = relationship(back_populates="paper_order")


class Positions(TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (Index("ix_positions_status_stock", "status", "stock_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id", ondelete="RESTRICT"), nullable=False, index=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    portfolio: Mapped["Portfolio"] = relationship(back_populates="positions")
    stock: Mapped[Stock] = relationship(back_populates="positions")
    history: Mapped[list["TradeHistory"]] = relationship(back_populates="position")
    orders: Mapped[list[PaperOrders]] = relationship(back_populates="position")


class TradeHistory(TimestampMixin, Base):
    __tablename__ = "trade_history"
    __table_args__ = (Index("ix_trade_history_position_created", "position_id", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    paper_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id", ondelete="SET NULL"), index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    details: Mapped[str | None] = mapped_column(Text)
    paper_order: Mapped[PaperOrders | None] = relationship(back_populates="history")
    position: Mapped[Positions | None] = relationship(back_populates="history")


class Watchlists(TimestampMixin, Base):
    __tablename__ = "watchlists"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    members: Mapped[list["WatchlistStocks"]] = relationship(back_populates="watchlist", cascade="all, delete-orphan")


class WatchlistStocks(Base):
    __tablename__ = "watchlist_stocks"
    __table_args__ = (UniqueConstraint("watchlist_id", "stock_id", name="uq_watchlist_stock"),)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id", ondelete="CASCADE"), primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    watchlist: Mapped[Watchlists] = relationship(back_populates="members")
    stock: Mapped[Stock] = relationship()


class Portfolio(TimestampMixin, Base):
    __tablename__ = "portfolio"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    positions: Mapped[list[Positions]] = relationship(back_populates="portfolio")


class TradeStatusChanges(Base):
    """Append-only audit trail for every paper order status transition."""
    __tablename__ = "trade_status_changes"
    __table_args__ = (Index("ix_trade_status_order_changed", "paper_order_id", "changed_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    paper_order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id", ondelete="CASCADE"), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(24))
    new_status: Mapped[str] = mapped_column(String(24), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    paper_order: Mapped[PaperOrders] = relationship(back_populates="status_changes")


class SystemLogs(Base):
    __tablename__ = "system_logs"
    __table_args__ = (Index("ix_system_logs_level_created", "level", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    level: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    context_json: Mapped[str | None] = mapped_column(Text)


class NotificationLogs(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (Index("ix_notification_logs_status_created", "status", "created_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class NSECalendarYear(Base):
    """A year for which an official NSE equity trading calendar was reviewed/imported."""
    __tablename__ = "nse_calendar_years"
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    source_published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    refreshed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    exceptions: Mapped[list["NSECalendarException"]] = relationship(
        back_populates="calendar_year", cascade="all, delete-orphan")


class NSECalendarException(Base):
    """Exchange holiday or special session override for the normal weekday calendar."""
    __tablename__ = "nse_calendar_exceptions"
    market_date: Mapped[date] = mapped_column(Date, primary_key=True)
    year: Mapped[int] = mapped_column(ForeignKey("nse_calendar_years.year", ondelete="CASCADE"),
                                      nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    opens_at: Mapped[datetime_time | None] = mapped_column(Time())
    closes_at: Mapped[datetime_time | None] = mapped_column(Time())
    calendar_year: Mapped[NSECalendarYear] = relationship(back_populates="exceptions")


class SchedulerRuns(Base):
    """Durable idempotency, run status, and audit record for scheduler operations."""
    __tablename__ = "scheduler_runs"
    __table_args__ = (Index("ix_scheduler_runs_name_date", "job_name", "market_date"),
                      Index("ix_scheduler_runs_status_started", "status", "started_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    run_key: Mapped[str] = mapped_column(String(220), nullable=False, unique=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    market_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner_id: Mapped[str | None] = mapped_column(String(160))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    details_json: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(120))


class SchedulerLeases(Base):
    """Cross-process lease for shared market-data pipeline operations."""
    __tablename__ = "scheduler_leases"
    resource: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(160), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)


class ApplicationRecord(Base):
    """Retained scaffold table for compatibility with existing local databases."""
    __tablename__ = "application_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@event.listens_for(Session, "before_flush")
def _audit_paper_order_status_changes(session: Session, _: object, __: object) -> None:
    """Capture status changes even when callers update orders outside repositories."""
    for instance in session.dirty:
        if not isinstance(instance, PaperOrders):
            continue
        status_history = inspect(instance).attrs.status.history
        if not status_history.has_changes():
            continue
        previous = status_history.deleted[0] if status_history.deleted else None
        current = status_history.added[0] if status_history.added else instance.status
        session.add(TradeStatusChanges(
            paper_order=instance,
            previous_status=previous,
            new_status=current,
        ))

