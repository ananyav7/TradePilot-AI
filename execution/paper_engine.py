"""Local-only paper execution with quote-based fills and a durable SQLite ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.connection import transaction
from database.models import Approvals, PaperOrders, Positions
from execution.order_manager import OrderManager, PaperOrderStatus
from execution.position_manager import PositionManager
from portfolio.portfolio_manager import PortfolioManager, PortfolioSnapshot
from notifications.telegram import TelegramNotifier
from risk.controls import KillSwitch
from risk.engine import (FeeSchedule, RiskConfig, RiskEngine, RiskRequest,
                         estimate_delivery_costs)


class PaperExecutionError(RuntimeError):
    """A local paper execution could not be accepted safely."""


@dataclass(frozen=True)
class ExecutionQuote:
    last_price: Decimal | float | str
    source: str
    observed_at: datetime
    bid: Decimal | float | str | None = None
    ask: Decimal | float | str | None = None

    def validate(self) -> tuple[Decimal, Decimal | None, Decimal | None]:
        last = self._price(self.last_price, "last_price")
        bid = self._price(self.bid, "bid") if self.bid is not None else None
        ask = self._price(self.ask, "ask") if self.ask is not None else None
        if not self.source.strip():
            raise ValueError("Quote source must be provided")
        if self.observed_at.tzinfo is None:
            raise ValueError("Quote timestamp must be timezone-aware")
        if bid is not None and ask is not None and bid > ask:
            raise ValueError("Quote bid cannot exceed ask")
        return last, bid, ask

    @staticmethod
    def _price(value, name: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except Exception as exc:
            raise ValueError(f"{name} must be a finite positive price") from exc
        if not result.is_finite() or result <= 0:
            raise ValueError(f"{name} must be a finite positive price")
        return result


@dataclass(frozen=True)
class DailyMarketBar:
    stock_id: int
    open: Decimal | float | str
    high: Decimal | float | str
    low: Decimal | float | str
    close: Decimal | float | str
    source: str
    observed_at: datetime
    completed: bool = True

    def validate(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        prices = tuple(ExecutionQuote._price(value, field) for value, field in (
            (self.open, "open"), (self.high, "high"), (self.low, "low"), (self.close, "close")))
        open_, high, low, close = prices
        if high < max(open_, close, low) or low > min(open_, close, high):
            raise ValueError("Daily OHLC values are inconsistent")
        if not self.source.strip() or self.observed_at.tzinfo is None:
            raise ValueError("Completed bar requires a source and timezone-aware timestamp")
        if not self.completed:
            raise ValueError("Stop/target monitoring requires a completed market bar")
        return prices


@dataclass(frozen=True)
class PaperFill:
    order_id: int
    side: str
    quantity: int
    fill_price: Decimal
    transaction_cost: Decimal
    slippage_cost: Decimal
    position_id: int
    status: str
    observed_at: datetime
    source: str


class PaperTradingEngine:
    """Approval-aware local paper simulator. It has no Upstox order dependency."""

    def __init__(self, session: Session, *, risk_config: RiskConfig | None = None,
                 slippage_bps: Decimal | str | float | None = None,
                 max_quote_age: timedelta | None = None,
                 kill_switch: KillSwitch | None = None,
                 notifier: TelegramNotifier | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.session = session
        self.risk_config = risk_config or RiskConfig.from_settings()
        self.fees: FeeSchedule = self.risk_config.fees
        settings = get_settings()
        slippage_bps = settings.paper_slippage_bps if slippage_bps is None else slippage_bps
        max_quote_age = (timedelta(seconds=settings.paper_max_quote_age_seconds)
                         if max_quote_age is None else max_quote_age)
        self.slippage_bps = Decimal(str(slippage_bps))
        if not self.slippage_bps.is_finite() or self.slippage_bps < 0 or self.slippage_bps > 1000:
            raise ValueError("slippage_bps must be between 0 and 1000")
        if max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be positive")
        self.max_quote_age = max_quote_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.portfolio = PortfolioManager(session, self.risk_config)
        self.orders = OrderManager(session, self.clock)
        self.positions = PositionManager(session, self.portfolio, self.fees)
        self.kill_switch = kill_switch or KillSwitch()
        self.notifier = notifier or TelegramNotifier(session)
        self.risk_engine = RiskEngine(self.risk_config, kill_switch=self.kill_switch)

    def create_buy_order(self, approval_id: int, quantity: int, *,
                         stop_loss: Decimal | str | float,
                         target_price: Decimal | str | float) -> PaperOrders:
        """Create a pending BUY attached to a stored BUY signal approval."""
        if self.kill_switch.is_active:
            raise PaperExecutionError("Risk kill switch is active; new BUY orders are disabled")
        return self.orders.create_buy_order(approval_id, quantity,
                                            stop_loss=stop_loss, target_price=target_price)

    def create_sell_order(self, position_id: int, quantity: int, *,
                          reason: str = "MANUAL_EXIT") -> PaperOrders:
        position = self.session.get(Positions, position_id)
        paper_portfolio = self.portfolio.get_or_create()
        if position is None or position.portfolio_id != paper_portfolio.id:
            raise LookupError("Position is not part of the local paper portfolio")
        return self.orders.create_sell_order(position_id, quantity, reason=reason)

    def approve_order(self, order_id: int, *, approved_by: str, reason: str = "User approved") -> PaperOrders:
        order = self.session.get(PaperOrders, order_id)
        if order is not None and order.side.upper() == "BUY" and self.kill_switch.is_active:
            raise PaperExecutionError("Risk kill switch is active; BUY approval is disabled")
        return self.orders.approve(order_id, approved_by=approved_by, reason=reason)

    def reject_order(self, order_id: int, *, reason: str) -> PaperOrders:
        order = self.orders.reject(order_id, reason=reason)
        self.notifier.notify("trade_rejected", f"Paper {order.side} order #{order.id} for {order.stock.symbol if order.stock else order.stock_id} was rejected: {reason[:300]}")
        return order

    def cancel_order(self, order_id: int, *, reason: str = "Cancelled by user") -> PaperOrders:
        return self.orders.cancel(order_id, reason=reason)

    def expire_order(self, order_id: int, *, reason: str = "Order expired") -> PaperOrders:
        return self.orders.expire(order_id, reason=reason)

    def fill_order(self, order_id: int, quote: ExecutionQuote, *, quantity: int | None = None,
                   max_age: timedelta | None = None) -> PaperFill:
        order = self.session.get(PaperOrders, order_id)
        if order is None:
            raise LookupError(f"Paper order {order_id} does not exist")
        if order.status not in (PaperOrderStatus.APPROVED.value, PaperOrderStatus.PARTIALLY_FILLED.value):
            raise ValueError("Paper order must be approved before a fill can be simulated")
        last, bid, ask = quote.validate()
        now = self._now()
        observed = quote.observed_at.astimezone(timezone.utc)
        age = now - observed
        if age < timedelta(0) or age > (max_age or self.max_quote_age):
            raise ValueError("Execution quote is stale or from the future")
        side = order.side.upper()
        reference = (ask or last) if side == "BUY" else (bid or last)
        fill_price = self._slipped(reference, side)
        fill_quantity = quantity if quantity is not None else order.quantity - order.filled_quantity
        if isinstance(fill_quantity, bool) or not isinstance(fill_quantity, int) or fill_quantity <= 0:
            raise ValueError("Fill quantity must be a positive integer")

        # Initialize the persistent account before the fill transaction so account
        # creation cannot commit or split a partially applied order fill.
        self.portfolio.get_or_create()
        with transaction(self.session):
            order = self.session.get(PaperOrders, order_id)
            if order is None:
                raise LookupError("Paper order disappeared before fill")
            if order.filled_quantity + fill_quantity > order.quantity:
                raise ValueError("Fill quantity exceeds order remaining quantity")
            if side == "BUY":
                position = self._fill_buy(order, fill_quantity, fill_price, reference, quote, observed, now)
                cost, _ = estimate_delivery_costs(fill_price, fill_price, fill_quantity, self.fees)
                slip_cost = max(fill_price - reference, Decimal("0")) * fill_quantity
            elif side == "SELL":
                position = self._fill_sell(order, fill_quantity, fill_price, reference, quote, observed)
                from risk.engine import estimate_sell_cost
                cost = estimate_sell_cost(fill_price, fill_quantity, self.fees)
                slip_cost = max(reference - fill_price, Decimal("0")) * fill_quantity
            else:
                raise ValueError("Unsupported paper order side")
            self.orders.record_fill(order, fill_quantity, fill_price, reason=f"Paper fill from {quote.source}")
            if side == "SELL" and position.status == "closed":
                self._cancel_related_buys(position.stock_id, exclude_order_id=order.id)
            self.session.flush()
            result = PaperFill(order.id, side, fill_quantity, fill_price, cost, slip_cost,
                               position.id, order.status, observed, quote.source)
        if order.status == PaperOrderStatus.FILLED.value:
            self.notifier.notify("paper_order_completed", f"Paper {side} completed for {fill_quantity} {order.stock.symbol if order.stock else ''} shares at ₹{fill_price}.")
        return result

    def manual_exit(self, position_id: int, quote: ExecutionQuote, *, quantity: int | None = None,
                    approved_by: str = "user") -> PaperFill:
        """Create, explicitly approve, and simulate a manual SELL paper order."""
        position = self.session.get(Positions, position_id)
        paper_portfolio = self.portfolio.get_or_create()
        if position is None or position.portfolio_id != paper_portfolio.id or position.status != "open":
            raise LookupError("An open paper position is required")
        order_quantity = position.quantity if quantity is None else quantity
        order = self.create_sell_order(position_id, order_quantity, reason="MANUAL_EXIT")
        self.approve_order(order.id, approved_by=approved_by, reason="Manual paper exit approved")
        return self.fill_order(order.id, quote, quantity=quantity)

    def monitor_stops(self, bar: DailyMarketBar) -> PaperFill | None:
        """Simulate a completed daily stop/target trigger, including gap-through fills."""
        open_, high, low, _ = bar.validate()
        paper_portfolio = self.portfolio.get_or_create()
        positions = list(self.session.scalars(select(Positions).where(
            Positions.portfolio_id == paper_portfolio.id, Positions.stock_id == bar.stock_id,
            Positions.status == "open")))
        if not positions:
            return None
        if len(positions) > 1:
            raise PaperExecutionError("Multiple open positions for one stock violate paper-account invariants")
        position = positions[0]
        stop = Decimal(position.stop_loss) if position.stop_loss is not None else None
        target = Decimal(position.target_price) if position.target_price is not None else None
        reason, trigger = self._trigger(open_, high, low, stop, target)
        if reason is None:
            return None
        order = self.create_sell_order(position.id, position.quantity, reason=reason)
        self.approve_order(order.id, approved_by="paper-protection", reason=f"Paper {reason} trigger")
        fill = self.fill_order(order.id, ExecutionQuote(trigger, bar.source, bar.observed_at),
                               max_age=self.risk_config.max_market_data_age)
        if fill is not None:
            event = "stop_loss_triggered" if reason == "STOP_LOSS" else "target_reached"
            self.notifier.notify(event, f"{position.stock.symbol}: paper {reason.lower().replace('_', ' ')} triggered; simulated exit at ₹{fill.fill_price}.")
        return fill

    def portfolio_snapshot(self, current_prices=None, portfolio_id: int | None = None) -> PortfolioSnapshot:
        return self.portfolio.snapshot(current_prices, portfolio_id)

    def _fill_buy(self, order: PaperOrders, quantity: int, price: Decimal,
                  reference: Decimal, quote: ExecutionQuote,
                  observed: datetime, now: datetime) -> Positions:
        if order.stop_loss is None or order.target_price is None:
            raise ValueError("Approved BUY is missing persisted stop-loss/target parameters")
        if self.kill_switch.is_active:
            raise PaperExecutionError("Risk kill switch is active; new BUY fills are disabled")
        if order.approval_id is None:
            raise PaperExecutionError("BUY paper order has no persisted signal approval")
        approval = self.session.get(Approvals, order.approval_id)
        if (approval is None or approval.signal.expires_at is not None and
                approval.signal.expires_at <= now):
            raise PaperExecutionError("Approved BUY signal expired before the paper fill")
        stop = self._slipped(Decimal(order.stop_loss), "SELL")
        target = self._slipped(Decimal(order.target_price), "SELL")
        if stop <= 0 or stop >= price or target <= price:
            raise PaperExecutionError("Observed price gap invalidates approved stop/target levels; order not filled")
        snapshot = self.portfolio.snapshot()
        current_position = self.session.get(Positions, order.position_id) if order.position_id else None
        existing_count = snapshot.open_positions
        if current_position is None and existing_count >= self.risk_config.max_open_positions:
            raise PaperExecutionError("Maximum one open position is already reached")
        if current_position is not None and current_position.stock_id != order.stock_id:
            raise PaperExecutionError("Paper order position does not match its stock")
        cash = snapshot.available_cash
        buy_fee, _ = estimate_delivery_costs(price, price, quantity, self.fees)
        required_cash = price * quantity + buy_fee
        if required_cash > cash:
            raise PaperExecutionError("Insufficient paper cash for this fill and estimated buy charges")
        if order.filled_quantity == 0:
            assessment = self.risk_engine.assess(RiskRequest(
                entry_price=price, stop_loss=stop, target_price=target,
                available_capital=self.risk_config.starting_capital,
                available_cash=cash, current_exposure=snapshot.invested_capital,
                open_positions=existing_count, daily_pnl=0,
                market_data_at=observed, as_of=now))
            if not assessment.accepted or order.quantity > assessment.quantity:
                raise PaperExecutionError("Approved quantity violates current risk limits: " +
                                          ", ".join(assessment.reasons or ("quantity_above_permitted_size",)))
        else:
            if current_position is None:
                raise PaperExecutionError("Partial BUY fill lost its linked position")
            next_qty = current_position.quantity + quantity
            next_avg = (Decimal(current_position.average_entry_price) * current_position.quantity + price * quantity) / next_qty
            stop_risk = max(next_avg - stop, Decimal("0")) * next_qty
            _, stop_costs = estimate_delivery_costs(next_avg, stop, next_qty, self.fees)
            if stop_risk + stop_costs > self.risk_config.starting_capital * self.risk_config.risk_per_trade:
                raise PaperExecutionError("Partial fill would exceed the configured maximum risk")
        return self.positions.apply_buy_fill(order, quantity, price, buy_fee,
                                             observed_at=observed, source=quote.source,
                                             slippage_amount=max(price - reference, Decimal("0")) * quantity)

    def _cancel_related_buys(self, stock_id: int | None, *, exclude_order_id: int) -> None:
        if stock_id is None:
            return
        active = list(self.session.scalars(select(PaperOrders).where(
            PaperOrders.stock_id == stock_id, PaperOrders.side == "BUY",
            PaperOrders.id != exclude_order_id,
            PaperOrders.status.in_((PaperOrderStatus.PENDING.value,
                                    PaperOrderStatus.APPROVED.value,
                                    PaperOrderStatus.PARTIALLY_FILLED.value)))))
        for order in active:
            order.status = PaperOrderStatus.CANCELLED.value
            self.session.flush()
            if order.status_changes:
                order.status_changes[-1].reason = "Open position closed; remaining BUY quantity cancelled"
        self.session.flush()

    def _fill_sell(self, order: PaperOrders, quantity: int, price: Decimal,
                   reference: Decimal, quote: ExecutionQuote, observed: datetime) -> Positions:
        if order.position_id is None:
            raise ValueError("SELL order must reference an open paper position")
        position = self.session.get(Positions, order.position_id)
        if position is None or position.status != "open":
            raise ValueError("SELL order position is no longer open")
        if quantity > position.quantity:
            raise ValueError("Sell fill exceeds remaining position quantity")
        self.positions.apply_sell_fill(order, position, quantity, price,
                                       observed_at=observed, source=quote.source,
                                       reason=order.reason or "APPROVED_SELL",
                                       slippage_amount=max(reference - price, Decimal("0")) * quantity)
        return position

    def _slipped(self, price: Decimal, side: str) -> Decimal:
        factor = Decimal("1") + self.slippage_bps / Decimal("10000")
        adjusted = price * factor if side.upper() == "BUY" else price / factor
        return adjusted.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _trigger(open_: Decimal, high: Decimal, low: Decimal,
                 stop: Decimal | None, target: Decimal | None) -> tuple[str | None, Decimal]:
        stop_hit = stop is not None and low <= stop
        target_hit = target is not None and high >= target
        if stop_hit and target_hit:
            return "STOP_LOSS", min(open_, stop)
        if stop_hit:
            return "STOP_LOSS", min(open_, stop)
        if target_hit:
            return "TARGET_REACHED", max(open_, target)
        return None, Decimal("0")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Paper engine clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)
