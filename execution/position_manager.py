"""Atomic position and fill-ledger updates for paper orders."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import PaperOrders, Positions, Stock, TradeHistory
from portfolio.portfolio_manager import PortfolioManager
from risk.engine import FeeSchedule, estimate_sell_cost


class PositionManager:
    def __init__(self, session: Session, portfolio: PortfolioManager,
                 fees: FeeSchedule) -> None:
        self.session = session
        self.portfolio = portfolio
        self.fees = fees

    def apply_buy_fill(self, order: PaperOrders, quantity: int, price: Decimal,
                       buy_fee: Decimal, *, observed_at: datetime, source: str,
                       slippage_amount: Decimal) -> Positions:
        portfolio = self.portfolio.get_or_create()
        if order.position_id is not None:
            position = self.session.get(Positions, order.position_id)
            if position is None or position.status != "open":
                raise ValueError("Order no longer references an open paper position")
        else:
            open_positions = list(self.session.scalars(select(Positions).where(
                Positions.portfolio_id == portfolio.id, Positions.status == "open")))
            if open_positions:
                if len(open_positions) >= 1:
                    raise ValueError("Maximum one open paper position is enforced")
            stock = self.session.get(Stock, order.stock_id)
            if stock is None:
                raise LookupError("Order stock does not exist")
            position = Positions(portfolio_id=portfolio.id, stock_id=order.stock_id,
                                 quantity=0, average_entry_price=Decimal("0"),
                                 stop_loss=order.stop_loss, target_price=order.target_price,
                                 status="open", opened_at=observed_at)
            self.session.add(position)
            self.session.flush()
            order.position_id = position.id
        old_quantity = position.quantity
        new_quantity = old_quantity + quantity
        old_cost = Decimal(position.average_entry_price) * old_quantity
        position.average_entry_price = (old_cost + price * quantity) / new_quantity
        position.quantity = new_quantity
        if position.stop_loss is None and order.stop_loss is not None:
            position.stop_loss = order.stop_loss
        if position.target_price is None and order.target_price is not None:
            position.target_price = order.target_price
        details = {"cash_delta": str(-(price * quantity + buy_fee)), "buy_fee": str(buy_fee),
                   "slippage_cost": str(slippage_amount), "source": source,
                   "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
                   "order_side": "BUY"}
        self.session.add(TradeHistory(paper_order_id=order.id, position_id=position.id,
                                      event_type="BUY_FILL", quantity=quantity, price=price,
                                      details=json.dumps(details, separators=(",", ":"))))
        return position

    def apply_sell_fill(self, order: PaperOrders, position: Positions, quantity: int,
                        price: Decimal, *, observed_at: datetime, source: str,
                        reason: str, slippage_amount: Decimal) -> dict:
        if position.status != "open" or quantity > position.quantity:
            raise ValueError("Sell quantity exceeds the available open position")
        remaining_before = position.quantity
        buy_fees, prior_allocations = self._buy_fee_ledger(position.id)
        available_buy_fees = max(buy_fees - prior_allocations, Decimal("0"))
        allocated_buy_fee = (available_buy_fees if quantity == remaining_before else
                             (available_buy_fees * quantity / remaining_before).quantize(
                                 Decimal("0.01"), rounding=ROUND_HALF_UP))
        sell_fee = estimate_sell_cost(price, quantity, self.fees)
        gross_pnl = (price - Decimal(position.average_entry_price)) * quantity
        realized_net = gross_pnl - sell_fee - allocated_buy_fee
        cash_delta = price * quantity - sell_fee
        position.quantity -= quantity
        if position.quantity == 0:
            position.status = "closed"
            position.closed_at = observed_at
        details = {"cash_delta": str(cash_delta), "sell_fee": str(sell_fee),
                   "allocated_buy_fee": str(allocated_buy_fee), "gross_pnl": str(gross_pnl),
                   "realized_net_pnl": str(realized_net), "slippage_cost": str(slippage_amount),
                   "source": source, "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
                   "reason": order.reason or reason, "order_side": "SELL"}
        self.session.add(TradeHistory(paper_order_id=order.id, position_id=position.id,
                                      event_type="SELL_FILL", quantity=quantity, price=price,
                                      details=json.dumps(details, separators=(",", ":"))))
        return {"sell_fee": sell_fee, "allocated_buy_fee": allocated_buy_fee,
                "gross_pnl": gross_pnl, "realized_net_pnl": realized_net,
                "cash_delta": cash_delta}

    def _buy_fee_ledger(self, position_id: int) -> tuple[Decimal, Decimal]:
        history = self.session.scalars(select(TradeHistory).where(
            TradeHistory.position_id == position_id,
            TradeHistory.event_type.in_(("BUY_FILL", "SELL_FILL"))))
        buy_fees, allocated = Decimal("0"), Decimal("0")
        for item in history:
            try:
                details = json.loads(item.details or "{}")
            except (ValueError, TypeError):
                details = {}
            if item.event_type == "BUY_FILL":
                buy_fees += Decimal(str(details.get("buy_fee", "0")))
            else:
                allocated += Decimal(str(details.get("allocated_buy_fee", "0")))
        return buy_fees, allocated
