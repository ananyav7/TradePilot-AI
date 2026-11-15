"""Local-only paper portfolio and order API; no broker order endpoints are used."""

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import TradeSignals
from execution.paper_engine import (DailyMarketBar, ExecutionQuote,
                                    PaperExecutionError, PaperTradingEngine)
from execution.approval_manager import ApprovalManager
from notifications.telegram import TelegramNotifier
from portfolio.portfolio_manager import PortfolioManager

router = APIRouter(prefix="/paper", tags=["paper-trading"])


class BuyOrderRequest(BaseModel):
    approval_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    stop_loss: Decimal = Field(gt=0)
    target_price: Decimal = Field(gt=0)


class SellOrderRequest(BaseModel):
    position_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    reason: str = "MANUAL_EXIT"


class QuoteRequest(BaseModel):
    last_price: Decimal = Field(gt=0)
    source: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)

    def to_quote(self) -> ExecutionQuote:
        return ExecutionQuote(self.last_price, self.source, self.observed_at, self.bid, self.ask)


class FillRequest(QuoteRequest):
    quantity: int | None = Field(default=None, gt=0)


class ManualExitRequest(FillRequest):
    approved_by: str = Field(default="user", min_length=1, max_length=80)


class DailyBarRequest(BaseModel):
    stock_id: int = Field(gt=0)
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    source: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    completed: bool = True


class DecisionRequest(BaseModel):
    actor: str = Field(default="user", min_length=1, max_length=80)
    reason: str = Field(default="User decision", max_length=2000)


class ApprovalReviewRequest(QuoteRequest):
    pass


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    signal_version: str = Field(min_length=1, max_length=160)
    confirm_text: str = Field(default="", max_length=200)
    quantity: int | None = Field(default=None, gt=0)
    actor: str = Field(default="user", min_length=1, max_length=80)
    comment: str = Field(default="", max_length=2000)
    last_price: Decimal | None = Field(default=None, gt=0)
    source: str | None = Field(default=None, min_length=1, max_length=120)
    observed_at: datetime | None = None
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)

    def to_quote(self) -> ExecutionQuote | None:
        if self.last_price is None and self.source is None and self.observed_at is None:
            return None
        if self.last_price is None or self.source is None or self.observed_at is None:
            raise ValueError("Approval requires a complete fresh sourced quote")
        return ExecutionQuote(self.last_price, self.source, self.observed_at, self.bid, self.ask)


def _engine(session: Session) -> PaperTradingEngine:
    return PaperTradingEngine(session)


def _order_payload(order) -> dict:
    return {"id": order.id, "approval_id": order.approval_id, "position_id": order.position_id,
            "stock_id": order.stock_id, "side": order.side, "quantity": order.quantity,
            "filled_quantity": order.filled_quantity, "average_price": order.average_price,
            "status": order.status, "stop_loss": order.stop_loss,
            "target_price": order.target_price, "reason": order.reason}


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PaperExecutionError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Paper operation failed")


@router.get("/portfolio")
def get_paper_portfolio(session: Session = Depends(get_db)) -> dict:
    try:
        return asdict(_engine(session).portfolio_snapshot())
    except (LookupError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.get("/approvals/opportunities")
def list_approval_opportunities(session: Session = Depends(get_db)) -> list[dict]:
    """List unexpired BUY candidates awaiting an explicit fresh-quote review."""
    now = datetime.now(timezone.utc)
    ApprovalManager(session).expire_due()
    signals = session.scalars(select(TradeSignals).where(
        TradeSignals.signal_type == "BUY", TradeSignals.expires_at > now,
        TradeSignals.status.notin_(("rejected", "expired"))).order_by(TradeSignals.created_at.desc()))
    return [{"signal_key": item.signal_key, "symbol": item.stock.symbol,
             "signal_date": item.signal_date, "expires_at": item.expires_at,
             "reference_price": item.reference_price,
             "reviewed": item.approval is not None,
             "approval_status": item.approval.status if item.approval else None}
            for item in signals if item.approval is None]


@router.post("/approvals/{signal_key}/review")
def review_signal(signal_key: str, request: ApprovalReviewRequest,
                  session: Session = Depends(get_db)) -> dict:
    try:
        from dataclasses import asdict
        review = ApprovalManager(session).review(signal_key, request.to_quote())
        return asdict(review)
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc


@router.post("/approvals/{approval_id}/decision")
def decide_signal(approval_id: int, request: ApprovalDecisionRequest,
                  session: Session = Depends(get_db)) -> dict:
    try:
        approval, order_id = ApprovalManager(session).decide(
            approval_id, decision=request.decision, expected_signal_version=request.signal_version,
            confirm_text=request.confirm_text, quote=request.to_quote(), quantity=request.quantity, actor=request.actor,
            comment=request.comment)
        return {"approval_id": approval.id, "signal_id": approval.signal.signal_key,
                "status": approval.status, "decision_at": approval.decision_at,
                "signal_version": approval.signal_version, "approved_quantity": approval.approved_quantity,
                "paper_order_id": order_id}
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc


@router.post("/notifications/daily-report")
def send_daily_paper_report(session: Session = Depends(get_db)) -> dict:
    """Manually send a daily paper portfolio summary; Telegram cannot trade."""
    snapshot = PortfolioManager(session).snapshot()
    message = (f"Daily paper report — capital ₹{snapshot.initial_capital:.2f}; "
               f"cash ₹{snapshot.available_cash:.2f}; invested ₹{snapshot.invested_capital:.2f}; "
               f"realised P&L ₹{snapshot.realized_pnl:.2f}; open positions {snapshot.open_positions}. "
               "Paper results only; no live orders.")
    record = TelegramNotifier(session).daily_report(message)
    return {"notification_id": record.id, "status": record.status}


@router.post("/orders/buy")
def create_buy_order(request: BuyOrderRequest, session: Session = Depends(get_db)) -> dict:
    try:
        order = _engine(session).create_buy_order(request.approval_id, request.quantity,
                                                 stop_loss=request.stop_loss,
                                                 target_price=request.target_price)
        return _order_payload(order)
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc


@router.post("/orders/sell")
def create_sell_order(request: SellOrderRequest, session: Session = Depends(get_db)) -> dict:
    try:
        order = _engine(session).create_sell_order(request.position_id, request.quantity,
                                                  reason=request.reason)
        return _order_payload(order)
    except (LookupError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/orders/{order_id}/approve")
def approve_order(order_id: int, request: DecisionRequest, session: Session = Depends(get_db)) -> dict:
    try:
        order = _engine(session).approve_order(order_id, approved_by=request.actor, reason=request.reason)
        return _order_payload(order)
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc


@router.post("/orders/{order_id}/reject")
def reject_order(order_id: int, request: DecisionRequest, session: Session = Depends(get_db)) -> dict:
    try:
        order = _engine(session).reject_order(order_id, reason=request.reason)
        return _order_payload(order)
    except (LookupError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, request: DecisionRequest, session: Session = Depends(get_db)) -> dict:
    try:
        order = _engine(session).cancel_order(order_id, reason=request.reason)
        return _order_payload(order)
    except (LookupError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/orders/{order_id}/expire")
def expire_order(order_id: int, request: DecisionRequest, session: Session = Depends(get_db)) -> dict:
    try:
        order = _engine(session).expire_order(order_id, reason=request.reason)
        return _order_payload(order)
    except (LookupError, ValueError) as exc:
        raise _translate_error(exc) from exc


@router.post("/orders/{order_id}/fill")
def fill_order(order_id: int, request: FillRequest, session: Session = Depends(get_db)) -> dict:
    try:
        fill = _engine(session).fill_order(order_id, request.to_quote(), quantity=request.quantity)
        return asdict(fill)
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc


@router.post("/positions/{position_id}/manual-exit")
def manual_exit(position_id: int, request: ManualExitRequest,
                session: Session = Depends(get_db)) -> dict:
    try:
        fill = _engine(session).manual_exit(position_id, request.to_quote(), quantity=request.quantity,
                                            approved_by=request.approved_by)
        return asdict(fill)
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc


@router.post("/positions/monitor")
def monitor_position(request: DailyBarRequest, session: Session = Depends(get_db)) -> dict | None:
    try:
        fill = _engine(session).monitor_stops(DailyMarketBar(
            request.stock_id, request.open, request.high, request.low, request.close,
            request.source, request.observed_at, request.completed))
        return asdict(fill) if fill is not None else None
    except (LookupError, ValueError, PaperExecutionError) as exc:
        raise _translate_error(exc) from exc
