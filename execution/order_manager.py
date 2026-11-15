"""Paper-order creation and audited lifecycle transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.orm import Session

from database.connection import transaction
from database.models import Approvals, PaperOrders, Positions, Stock, TradeStatusChanges, TradeSignals
from execution.signal_version import signal_version


class PaperOrderStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


TERMINAL_STATUSES = {PaperOrderStatus.FILLED, PaperOrderStatus.REJECTED,
                     PaperOrderStatus.CANCELLED, PaperOrderStatus.EXPIRED}
ALLOWED_TRANSITIONS = {
    PaperOrderStatus.PENDING: {PaperOrderStatus.APPROVED, PaperOrderStatus.REJECTED,
                               PaperOrderStatus.CANCELLED, PaperOrderStatus.EXPIRED},
    PaperOrderStatus.APPROVED: {PaperOrderStatus.PARTIALLY_FILLED, PaperOrderStatus.FILLED,
                                PaperOrderStatus.REJECTED, PaperOrderStatus.CANCELLED,
                                PaperOrderStatus.EXPIRED},
    PaperOrderStatus.PARTIALLY_FILLED: {PaperOrderStatus.PARTIALLY_FILLED,
                                        PaperOrderStatus.FILLED, PaperOrderStatus.CANCELLED,
                                        PaperOrderStatus.EXPIRED},
}


class OrderManager:
    def __init__(self, session: Session, clock=None) -> None:
        self.session = session
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_buy_order(self, approval_id: int, quantity: int, *,
                         stop_loss: Decimal | str | float,
                         target_price: Decimal | str | float) -> PaperOrders:
        approval = self.session.get(Approvals, approval_id)
        if approval is None:
            raise LookupError("A persisted strategy approval is required for a BUY paper order")
        if approval.status != "approved" or approval.decision_at is None:
            raise ValueError("A BUY paper order requires a recorded explicit user approval decision")
        if not approval.signal_version or approval.approved_quantity is None:
            raise ValueError("A BUY paper order requires a versioned risk review snapshot")
        if approval.approved_quantity is not None and quantity > approval.approved_quantity:
            raise ValueError("Order quantity exceeds the risk-qualified approved quantity")
        signal = self.session.get(TradeSignals, approval.signal_id)
        if signal is None or signal.signal_type != "BUY":
            raise ValueError("Paper BUY orders must reference a BUY strategy signal")
        if signal_version(signal) != approval.signal_version:
            raise ValueError("Strategy signal changed after approval; a fresh review is required")
        if signal.expires_at is not None and signal.expires_at <= self._now():
            raise ValueError("The approved strategy signal has expired")
        stock_id = signal.stock_id
        return self._create("BUY", stock_id, quantity, approval_id=approval_id,
                            stop_loss=stop_loss, target_price=target_price)

    def create_sell_order(self, position_id: int, quantity: int, *, reason: str = "MANUAL_EXIT") -> PaperOrders:
        position = self.session.get(Positions, position_id)
        if position is None or position.status != "open":
            raise LookupError("An open paper position is required for a SELL order")
        if quantity > position.quantity:
            raise ValueError("Sell quantity exceeds the open position")
        return self._create("SELL", position.stock_id, quantity, position_id=position.id,
                            reason=reason)

    def _create(self, side: str, stock_id: int, quantity: int, *, approval_id: int | None = None,
                position_id: int | None = None, stop_loss=None, target_price=None,
                reason: str | None = None) -> PaperOrders:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Order quantity must be a positive integer")
        if self.session.get(Stock, stock_id) is None:
            raise LookupError("Order stock does not exist")
        stop = Decimal(str(stop_loss)) if stop_loss is not None else None
        target = Decimal(str(target_price)) if target_price is not None else None
        if side == "BUY" and (stop is None or target is None or not stop.is_finite() or not target.is_finite() or stop <= 0 or target <= 0):
            raise ValueError("BUY orders require finite positive stop-loss and target levels")
        order = PaperOrders(approval_id=approval_id, position_id=position_id, stock_id=stock_id,
                            side=side, quantity=quantity, order_type="market",
                            status=PaperOrderStatus.PENDING.value, stop_loss=stop,
                            target_price=target, filled_quantity=0, reason=reason)
        with transaction(self.session):
            self.session.add(order)
            self.session.flush()
            self.session.add(TradeStatusChanges(paper_order_id=order.id, previous_status=None,
                                                new_status=PaperOrderStatus.PENDING.value,
                                                changed_at=self._now(), reason=reason or "Order created"))
            self.session.flush()
        return order

    def approve(self, order_id: int, *, approved_by: str, reason: str = "User approved paper order") -> PaperOrders:
        return self._transition(order_id, PaperOrderStatus.APPROVED, reason, actor=approved_by)

    def reject(self, order_id: int, *, reason: str) -> PaperOrders:
        return self._transition(order_id, PaperOrderStatus.REJECTED, reason)

    def cancel(self, order_id: int, *, reason: str = "Cancelled by user") -> PaperOrders:
        return self._transition(order_id, PaperOrderStatus.CANCELLED, reason)

    def expire(self, order_id: int, *, reason: str = "Order expired") -> PaperOrders:
        return self._transition(order_id, PaperOrderStatus.EXPIRED, reason)

    def record_fill(self, order: PaperOrders, quantity: int, price: Decimal,
                    *, reason: str = "Paper fill") -> None:
        if order.status not in (PaperOrderStatus.APPROVED.value, PaperOrderStatus.PARTIALLY_FILLED.value):
            raise ValueError("Only approved or partially filled paper orders can receive fills")
        remaining = order.quantity - order.filled_quantity
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or quantity > remaining:
            raise ValueError("Fill quantity must be positive and no greater than order remaining quantity")
        previous_quantity = order.filled_quantity
        next_quantity = previous_quantity + quantity
        prior_average = Decimal(order.average_price) if order.average_price is not None else Decimal("0")
        order.average_price = (prior_average * previous_quantity + price * quantity) / next_quantity
        order.filled_quantity = next_quantity
        target = (PaperOrderStatus.FILLED if next_quantity == order.quantity
                  else PaperOrderStatus.PARTIALLY_FILLED)
        order.status = target.value
        self.session.flush()
        if order.status_changes:
            order.status_changes[-1].reason = reason[:2000]

    def _transition(self, order_id: int, target: PaperOrderStatus,
                    reason: str, *, actor: str | None = None) -> PaperOrders:
        order = self.session.get(PaperOrders, order_id)
        if order is None:
            raise LookupError(f"Paper order {order_id} does not exist")
        try:
            current = PaperOrderStatus(order.status.upper())
        except ValueError as exc:
            raise ValueError(f"Unsupported legacy paper-order status: {order.status}") from exc
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ValueError(f"Invalid paper-order transition {current.value} -> {target.value}")
        if target == PaperOrderStatus.APPROVED and current == PaperOrderStatus.PENDING:
            if order.side == "BUY":
                if order.approval_id is None:
                    raise ValueError("BUY approval must be linked to a persisted user approval")
                approval = self.session.get(Approvals, order.approval_id)
                if approval is None:
                    raise LookupError("Order approval record does not exist")
                if approval.signal.signal_type != "BUY":
                    raise ValueError("Paper BUY approval must reference a BUY signal")
                if approval.signal.expires_at is not None and approval.signal.expires_at <= self._now():
                    raise ValueError("Approved strategy signal expired before order approval")
                approval.status = "approved"
                approval.decision_at = self._now()
                approval.decided_by = (actor or "user")[:80]
                if reason:
                    approval.comment = reason[:2000]
        with transaction(self.session):
            order.status = target.value
            self.session.flush()  # model listener records the status transition
            if order.status_changes:
                order.status_changes[-1].reason = reason[:2000] if reason else None
            self.session.flush()
        return order

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("Order lifecycle timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)
