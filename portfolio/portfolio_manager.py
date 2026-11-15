"""Persistent paper-portfolio balances derived from the append-only fill ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.connection import transaction
from database.models import Portfolio, Positions, Stock, TradeHistory
from risk.engine import RiskConfig

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PositionView:
    position_id: int
    stock_id: int
    symbol: str
    quantity: int
    average_entry_price: Decimal
    market_price: Decimal | None
    invested_capital: Decimal
    unrealized_pnl: Decimal | None
    stop_loss: Decimal | None
    target_price: Decimal | None


@dataclass(frozen=True)
class PortfolioSnapshot:
    portfolio_id: int
    name: str
    currency: str
    initial_capital: Decimal
    available_cash: Decimal
    invested_capital: Decimal
    total_value: Decimal | None
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    open_positions: int
    updated_at: datetime
    positions: tuple[PositionView, ...]


class PortfolioManager:
    """Manage one local virtual account; every balance is recoverable from SQLite."""

    def __init__(self, session: Session, risk_config: RiskConfig | None = None,
                 portfolio_name: str = "Paper Portfolio") -> None:
        self.session = session
        self.risk_config = risk_config or RiskConfig.from_settings()
        self.portfolio_name = portfolio_name

    def get_or_create(self) -> Portfolio:
        portfolio = self.session.scalar(select(Portfolio).where(Portfolio.name == self.portfolio_name))
        if portfolio is not None:
            return portfolio
        portfolio = Portfolio(name=self.portfolio_name, base_currency="INR",
                              starting_capital=self.risk_config.starting_capital)
        try:
            with transaction(self.session):
                self.session.add(portfolio)
                self.session.flush()
        except IntegrityError:
            # A concurrent local startup may have initialized the unique portfolio first.
            self.session.rollback()
            portfolio = self.session.scalar(select(Portfolio).where(Portfolio.name == self.portfolio_name))
            if portfolio is None:
                raise
        return portfolio

    def available_cash(self, portfolio_id: int | None = None) -> Decimal:
        portfolio = self.get_or_create() if portfolio_id is None else self.session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise LookupError("Paper portfolio does not exist")
        return self._cash_balance(portfolio)

    def snapshot(self, current_prices: dict[int, Decimal | int | float | str] | None = None,
                 portfolio_id: int | None = None) -> PortfolioSnapshot:
        portfolio = self.get_or_create() if portfolio_id is None else self.session.get(Portfolio, portfolio_id)
        if portfolio is None:
            raise LookupError("Paper portfolio does not exist")
        prices = {int(key): self._positive_price(value) for key, value in (current_prices or {}).items()}
        positions = list(self.session.scalars(select(Positions).where(
            Positions.portfolio_id == portfolio.id, Positions.status == "open").order_by(Positions.id)))
        views: list[PositionView] = []
        invested = ZERO
        unrealized = ZERO
        all_prices_available = True
        for position in positions:
            stock = self.session.get(Stock, position.stock_id)
            avg = Decimal(position.average_entry_price)
            invested_amount = avg * position.quantity
            invested += invested_amount
            price = prices.get(position.stock_id)
            pnl = (price - avg) * position.quantity if price is not None else None
            if pnl is None:
                all_prices_available = False
            else:
                unrealized += pnl
            views.append(PositionView(position.id, position.stock_id, stock.symbol if stock else "UNKNOWN",
                                      position.quantity, avg, price, invested_amount, pnl,
                                      Decimal(position.stop_loss) if position.stop_loss is not None else None,
                                      Decimal(position.target_price) if position.target_price is not None else None))
        cash = self._cash_balance(portfolio)
        realized = self._realized_balance(portfolio)
        unrealized_value = unrealized if all_prices_available else None
        total_value = cash + invested if unrealized_value is None else cash + invested + unrealized_value
        return PortfolioSnapshot(portfolio.id, portfolio.name, portfolio.base_currency,
                                 Decimal(portfolio.starting_capital), cash, invested, total_value,
                                 realized, unrealized_value, len(positions), portfolio.updated_at,
                                 tuple(views))

    def _cash_balance(self, portfolio: Portfolio) -> Decimal:
        history = self.session.scalars(select(TradeHistory).join(
            Positions, TradeHistory.position_id == Positions.id).where(
                Positions.portfolio_id == portfolio.id,
                TradeHistory.event_type.in_(("BUY_FILL", "SELL_FILL"))))
        cash = Decimal(portfolio.starting_capital)
        for record in history:
            details = self._details(record.details)
            cash += Decimal(str(details.get("cash_delta", "0")))
        return cash.quantize(Decimal("0.01"))

    def _realized_balance(self, portfolio: Portfolio) -> Decimal:
        history = self.session.scalars(select(TradeHistory).join(
            Positions, TradeHistory.position_id == Positions.id).where(
                Positions.portfolio_id == portfolio.id, TradeHistory.event_type == "SELL_FILL"))
        return sum((Decimal(str(self._details(item.details).get("realized_net_pnl", "0")))
                    for item in history), ZERO).quantize(Decimal("0.01"))

    @staticmethod
    def _details(value: str | None) -> dict:
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _positive_price(value) -> Decimal:
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("Current prices must be finite and positive") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Current prices must be finite and positive")
        return price
