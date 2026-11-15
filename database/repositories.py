"""Small SQLAlchemy repositories with transaction-safe write operations."""

from datetime import datetime
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.connection import transaction
from database.models import (
    Approvals, Indicators, MarketData, NotificationLogs, PaperOrders, Portfolio,
    Positions, Stock, SystemLogs, TradeHistory, TradeSignals,
    Watchlists, WatchlistStocks,
)

Entity = TypeVar("Entity")


class Repository(Generic[Entity]):
    """Generic repository; writes are committed atomically and roll back on errors."""

    model: type[Entity]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: int) -> Entity | None:
        return self.session.get(self.model, entity_id)

    def list(self, *, limit: int = 100, offset: int = 0) -> list[Entity]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset cannot be negative")
        statement = select(self.model).offset(offset).limit(limit)
        return list(self.session.scalars(statement))

    def add(self, entity: Entity) -> Entity:
        with transaction(self.session):
            self.session.add(entity)
            self.session.flush()
        return entity

    def delete(self, entity: Entity) -> None:
        with transaction(self.session):
            self.session.delete(entity)


class StockRepository(Repository[Stock]):
    model = Stock


class MarketDataRepository(Repository[MarketData]):
    model = MarketData


class IndicatorsRepository(Repository[Indicators]):
    model = Indicators
class TradeSignalRepository(Repository[TradeSignals]):
    model = TradeSignals

    def get_for_candle(self, stock_id: int, candle_at: datetime) -> TradeSignals | None:
        return self.session.scalar(select(TradeSignals).where(
            TradeSignals.stock_id == stock_id, TradeSignals.candle_at == candle_at))

    def save_once(self, signal: TradeSignals) -> tuple[TradeSignals, bool]:
        """Persist one immutable strategy snapshot per stock/candle, including under races."""
        if signal.candle_at is None:
            raise ValueError("Strategy signal requires a completed candle timestamp")
        existing = self.get_for_candle(signal.stock_id, signal.candle_at)
        if existing is not None:
            return existing, False
        try:
            with transaction(self.session):
                self.session.add(signal)
                self.session.flush()
            return signal, True
        except IntegrityError:
            existing = self.get_for_candle(signal.stock_id, signal.candle_at)
            if existing is not None:
                return existing, False
            raise
class ApprovalRepository(Repository[Approvals]):
    model = Approvals


class _PaperOrderRepository(Repository[PaperOrders]):
    model = PaperOrders


class PositionRepository(Repository[Positions]):
    model = Positions


class TradeHistoryRepository(Repository[TradeHistory]):
    model = TradeHistory


class WatchlistRepository(Repository[Watchlists]):
    model = Watchlists


class WatchlistStockRepository(Repository[WatchlistStocks]):
    model = WatchlistStocks


class PortfolioRepository(Repository[Portfolio]):
    model = Portfolio


class SystemLogRepository(Repository[SystemLogs]):
    model = SystemLogs


class NotificationLogRepository(Repository[NotificationLogs]):
    model = NotificationLogs


class PaperOrderRepository(_PaperOrderRepository):
    """Paper order writes, including append-only status transition auditing."""

    def set_status(self, order_id: int, status: str, reason: str | None = None) -> PaperOrders:
        order = self.session.get(PaperOrders, order_id)
        if order is None:
            raise LookupError(f"Paper order {order_id} does not exist")
        with transaction(self.session):
            order.status = status
            self.session.flush()
            if reason and order.status_changes:
                order.status_changes[-1].reason = reason
        return order

