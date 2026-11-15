"""Risk-qualified manual signal review and one-time decision workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.connection import transaction
from database.models import (Approvals, PaperOrders, Positions, TradeHistory, TradeSignals,
                             TradeStatusChanges)
from execution.paper_engine import ExecutionQuote, PaperExecutionError, PaperTradingEngine
from execution.signal_version import signal_version
from notifications.telegram import TelegramNotifier
from risk.engine import RiskAssessment, RiskRequest, estimate_delivery_costs


@dataclass(frozen=True)
class ApprovalReview:
    approval_id: int
    signal_key: str
    symbol: str
    signal_version: str
    entry_price: Decimal
    quantity: int
    stop_loss: Decimal
    target_price: Decimal
    risk_amount: Decimal
    estimated_charges: Decimal
    risk_reward: Decimal | None
    explanation: str
    quote_source: str
    quote_at: datetime


class ApprovalManager:
    """Require a fresh quote and an explicit decision for every persisted signal."""

    def __init__(self, session: Session, *, engine: PaperTradingEngine | None = None,
                 notifier: TelegramNotifier | None = None, clock=None) -> None:
        self.session = session
        self.engine = engine or PaperTradingEngine(session, clock=clock)
        self.notifier = notifier or TelegramNotifier(session)
        self.clock = clock or self.engine.clock

    def review(self, signal_key: str, quote: ExecutionQuote) -> ApprovalReview:
        self.expire_due()
        signal = self._signal(signal_key)
        if self._expired(signal):
            raise ValueError("Strategy signal has expired")
        version = self.signal_version(signal)
        entry = self._fresh_price(quote)
        assessment = self._assess(signal, entry, quote.observed_at)
        if not assessment.accepted:
            raise PaperExecutionError("Signal failed current risk checks: " + ", ".join(assessment.reasons))
        existing = self.session.scalar(select(Approvals).where(Approvals.signal_id == signal.id))
        if existing is not None:
            raise ValueError(f"This signal already has a recorded {existing.status} decision/workflow")
        approval = Approvals(signal_id=signal.id, status="pending", signal_version=version,
                             approved_quantity=assessment.quantity,
                             decision_entry_price=entry, decision_stop_loss=signal.stop_loss,
                             decision_target_price=signal.target_price,
                             risk_amount=assessment.risk_amount,
                             estimated_charges=assessment.round_trip_transaction_cost,
                             market_data_source=quote.source,
                             market_data_at=quote.observed_at.astimezone(timezone.utc))
        try:
            with transaction(self.session):
                self.session.add(approval)
                self.session.flush()
        except IntegrityError as exc:
            raise ValueError("Signal already has an approval record") from exc
        self.notifier.notify("new_opportunity", self._review_message(signal, assessment, entry))
        return self._review_payload(signal, approval, assessment, entry, quote)

    def expire_due(self) -> int:
        """Persist expired BUY opportunities and notify once for each signal."""
        now = self._now()
        expired = list(self.session.scalars(select(TradeSignals).where(
            TradeSignals.signal_type == "BUY", TradeSignals.expires_at.is_not(None),
            TradeSignals.expires_at <= now, TradeSignals.status.notin_(("expired", "rejected", "approved")))))
        notices: list[str] = []
        with transaction(self.session):
            for signal in expired:
                signal.status = "expired"
                approval = signal.approval
                if approval is None:
                    approval = Approvals(signal_id=signal.id, status="expired",
                                         signal_version=self.signal_version(signal),
                                         decision_at=now, comment="Signal expired without user approval")
                    self.session.add(approval)
                elif approval.status == "pending":
                    approval.status = "expired"
                    approval.decision_at = now
                    approval.comment = "Signal expired before user decision"
                notices.append(signal.stock.symbol)
            self.session.flush()
        for symbol in notices:
            self.notifier.notify("signal_expired", f"{symbol}: strategy signal expired; no paper order created.")
        return len(notices)

    def decide(self, approval_id: int, *, decision: str, expected_signal_version: str,
               quote: ExecutionQuote | None, confirm_text: str = "", quantity: int | None = None,
               actor: str = "user", comment: str = "") -> tuple[Approvals, int | None]:
        approval = self.session.get(Approvals, approval_id)
        if approval is None:
            raise LookupError("Approval review was not found")
        signal = approval.signal
        if approval.status != "pending" or approval.decision_at is not None:
            raise ValueError("This signal already has a final decision")
        if self._expired(signal):
            with transaction(self.session):
                approval.status = "expired"
                approval.decision_at = self._now()
                approval.comment = "Signal expired before decision"
                signal.status = "expired"
            self.notifier.notify("signal_expired", f"{signal.stock.symbol}: approval window expired; no order created.")
            return approval, None
        current_version = self.signal_version(signal)
        if (not expected_signal_version or expected_signal_version != approval.signal_version or
                current_version != approval.signal_version):
            raise ValueError("Signal changed since review; prepare a fresh review")
        choice = decision.strip().lower()
        if choice not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
        expected_confirmation = f"{choice.upper()} {signal.signal_key}"
        if confirm_text.strip() != expected_confirmation:
            raise ValueError(f"Fresh confirmation required: type {expected_confirmation}")
        if choice == "reject":
            with transaction(self.session):
                approval.status = "rejected"
                approval.decision_at = self._now()
                approval.decided_by = actor[:80]
                approval.comment = comment[:2000] or "Rejected by user"
                signal.status = "rejected"
            self.notifier.notify("trade_rejected", f"{signal.stock.symbol}: trade signal rejected by {actor}.")
            return approval, None

        if self.engine.kill_switch.is_active:
            raise PaperExecutionError("Risk kill switch is active; approval cannot create a paper order")
        if quote is None:
            raise ValueError("Approval requires a fresh sourced market quote")
        entry = self._fresh_price(quote)
        assessment = self._assess(signal, entry, quote.observed_at)
        if not assessment.accepted:
            raise PaperExecutionError("Signal failed current risk checks: " + ", ".join(assessment.reasons))
        approved_quantity = assessment.quantity if quantity is None else quantity
        if (isinstance(approved_quantity, bool) or not isinstance(approved_quantity, int) or
                approved_quantity <= 0 or approved_quantity > assessment.quantity):
            raise ValueError(f"Quantity must be between 1 and the risk-qualified maximum {assessment.quantity}")
        # Re-assess exact user-selected size; a size reduction is valid, an increase is not.
        exact = self._assess(signal, entry, quote.observed_at, quantity=approved_quantity)
        with transaction(self.session):
            approval.approved_quantity = approved_quantity
            approval.decision_entry_price = entry
            approval.decision_stop_loss = Decimal(signal.stop_loss)
            approval.decision_target_price = Decimal(signal.target_price)
            approval.risk_amount = exact.risk_amount
            approval.estimated_charges = exact.round_trip_transaction_cost
            approval.market_data_source = quote.source
            approval.market_data_at = quote.observed_at.astimezone(timezone.utc)
            approval.status = "approved"
            approval.decision_at = self._now()
            approval.decided_by = actor[:80]
            approval.comment = comment[:2000] or "Explicit user approval"
            order = PaperOrders(approval_id=approval.id, stock_id=signal.stock_id,
                                side="BUY", quantity=approved_quantity, order_type="market",
                                status="PENDING", filled_quantity=0,
                                stop_loss=signal.stop_loss, target_price=signal.target_price,
                                reason="SIGNAL_APPROVAL")
            self.session.add(order)
            self.session.flush()
            self.session.add(TradeStatusChanges(paper_order_id=order.id, previous_status=None,
                                                new_status="PENDING", changed_at=self._now(),
                                                reason="Created from explicit signal approval"))
            order.status = "APPROVED"
            self.session.flush()
            if order.status_changes:
                order.status_changes[-1].reason = "Explicit user signal approval"
            signal.status = "approved"
            self.session.flush()
        self.notifier.notify("approval_received", f"{signal.stock.symbol}: explicitly approved for {approved_quantity} paper shares. Paper order #{order.id} awaits a sourced fill quote.")
        return approval, order.id

    def _assess(self, signal: TradeSignals, entry: Decimal, market_at: datetime,
                quantity: int | None = None) -> RiskAssessment:
        if signal.stop_loss is None or signal.target_price is None:
            raise PaperExecutionError("Signal lacks stored stop-loss or target values")
        snapshot = self.engine.portfolio.snapshot()
        result = self.engine.risk_engine.assess(RiskRequest(
            entry_price=entry, stop_loss=signal.stop_loss, target_price=signal.target_price,
            available_capital=self.engine.risk_config.starting_capital,
            available_cash=snapshot.available_cash, current_exposure=snapshot.invested_capital,
            open_positions=snapshot.open_positions, daily_pnl=self._today_realized_pnl(),
            market_data_at=market_at, as_of=self._now()))
        if quantity is None:
            return result
        if result.accepted and quantity > result.quantity:
            raise ValueError("Requested quantity exceeds current risk-qualified maximum")
        if not result.accepted:
            return result
        buy_cost, round_trip = estimate_delivery_costs(entry, signal.target_price, quantity,
                                                       self.engine.fees)
        risk_cost = estimate_delivery_costs(entry, signal.stop_loss, quantity,
                                            self.engine.fees)[1]
        risk_amount = ((entry - Decimal(signal.stop_loss)) * quantity + risk_cost).quantize(Decimal("0.01"))
        return replace(result, quantity=quantity, risk_amount=risk_amount,
                       capital_requirement=(entry * quantity + buy_cost).quantize(Decimal("0.01")),
                       buy_transaction_cost=buy_cost, round_trip_transaction_cost=round_trip,
                       gross_expected_reward=(Decimal(signal.target_price) - entry) * quantity,
                       net_expected_reward=(Decimal(signal.target_price) - entry) * quantity - round_trip)

    def _fresh_price(self, quote: ExecutionQuote) -> Decimal:
        last, _, ask = quote.validate()
        observed = quote.observed_at.astimezone(timezone.utc)
        age = self._now() - observed
        if age < timedelta(0) or age > self.engine.max_quote_age:
            raise ValueError("Approval requires a fresh market price; quote is stale or from the future")
        return ask or last

    def _signal(self, signal_key: str) -> TradeSignals:
        signal = self.session.scalar(select(TradeSignals).where(TradeSignals.signal_key == signal_key))
        if signal is None:
            raise LookupError("Strategy signal was not found")
        if signal.signal_type != "BUY":
            raise ValueError("Only qualified BUY signals can enter trade approval")
        return signal

    def _expired(self, signal: TradeSignals) -> bool:
        return signal.expires_at is not None and signal.expires_at <= self._now()

    def signal_version(self, signal: TradeSignals) -> str:
        return signal_version(signal)

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("Approval clock must be timezone-aware")
        return now.astimezone(timezone.utc)

    def _today_realized_pnl(self) -> Decimal:
        india = ZoneInfo("Asia/Kolkata")
        local_now = self._now().astimezone(india)
        start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        history = self.session.scalars(select(TradeHistory).join(
            Positions, TradeHistory.position_id == Positions.id).where(
                Positions.portfolio_id == self.engine.portfolio.get_or_create().id,
                TradeHistory.event_type == "SELL_FILL", TradeHistory.created_at >= start_utc))
        realized = Decimal("0")
        for item in history:
            try:
                details = json.loads(item.details or "{}")
                realized += Decimal(str(details.get("realized_net_pnl", "0")))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return realized

    @staticmethod
    def _review_payload(signal, approval, assessment, entry, quote) -> ApprovalReview:
        return ApprovalReview(approval.id, signal.signal_key or str(signal.id), signal.stock.symbol,
                              approval.signal_version, entry, assessment.quantity,
                              Decimal(signal.stop_loss), Decimal(signal.target_price),
                              assessment.risk_amount, assessment.round_trip_transaction_cost,
                              assessment.risk_reward_ratio, signal.rationale or "No explanation was stored.",
                              quote.source, quote.observed_at.astimezone(timezone.utc))

    @staticmethod
    def _review_message(signal, risk, entry) -> str:
        return (f"{signal.stock.symbol}: new risk-qualified BUY opportunity for human review. "
                f"Reference ₹{entry}; max quantity {risk.quantity}; stop ₹{signal.stop_loss}; "
                f"target ₹{signal.target_price}. No trade is approved or placed.")
