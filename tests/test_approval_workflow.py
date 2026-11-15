from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from database.connection import Base
from database.models import Approvals, PaperOrders, Stock, TradeSignals
from execution.approval_manager import ApprovalManager
from execution.paper_engine import ExecutionQuote, PaperExecutionError, PaperTradingEngine
from risk.controls import KillSwitch
from risk.engine import RiskConfig


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)


class MockNotifier:
    def __init__(self):
        self.events = []

    def notify(self, event, message):
        self.events.append((event, message))


@pytest.fixture
def session():
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(db)
    with Session(db, expire_on_commit=False) as value:
        yield value
    db.dispose()


@pytest.fixture
def signal(session):
    stock = Stock(symbol="APPROVE", exchange="NSE", name="Approval Test Ltd")
    session.add(stock)
    session.flush()
    row = TradeSignals(stock_id=stock.id, signal_key="signal-approve-1", signal_type="BUY",
                       status="pending", reference_price=Decimal("100"), stop_loss=Decimal("95"),
                       target_price=Decimal("110"), quantity_placeholder=None,
                       rationale="EMA20 above EMA50; RSI and volume passed.",
                       expires_at=NOW + timedelta(minutes=5), strategy_version="swing-v1")
    session.add(row)
    session.commit()
    return row


def make_manager(session, notifier=None, kill_switch=None):
    alert = notifier or MockNotifier()
    engine = PaperTradingEngine(session, risk_config=RiskConfig(), slippage_bps=0,
                                clock=lambda: NOW, kill_switch=kill_switch, notifier=alert)
    return ApprovalManager(session, engine=engine, notifier=alert, clock=lambda: NOW)


def quote(at=NOW):
    return ExecutionQuote("100", "Manual verified quote", at, bid="99.9", ask="100.1")


def test_approval_creates_order_only_after_versioned_explicit_confirmation(session, signal):
    notifier = MockNotifier()
    manager = make_manager(session, notifier)
    review = manager.review(signal.signal_key, quote())
    assert review.quantity > 0
    assert review.signal_version.startswith("swing-v1:")
    approval = session.get(Approvals, review.approval_id)
    assert approval.status == "pending"
    assert session.query(PaperOrders).count() == 0

    with pytest.raises(ValueError, match="Fresh confirmation"):
        manager.decide(approval.id, decision="approve", expected_signal_version=review.signal_version,
                       quote=quote(), quantity=review.quantity)
    stored, order_id = manager.decide(
        approval.id, decision="approve", expected_signal_version=review.signal_version,
        confirm_text=f"APPROVE {signal.signal_key}", quote=quote(), quantity=review.quantity,
        actor="test-user")
    order = session.get(PaperOrders, order_id)
    assert stored.status == "approved" and stored.decision_at == NOW
    assert stored.signal_version == review.signal_version
    assert stored.approved_quantity == review.quantity
    assert order.status == "APPROVED"
    assert order.quantity == review.quantity
    assert notifier.events[-1][0] == "approval_received"


def test_rejection_is_persisted_without_creating_order_or_needing_quote(session, signal):
    notifier = MockNotifier()
    manager = make_manager(session, notifier)
    review = manager.review(signal.signal_key, quote())
    approval, order_id = manager.decide(
        review.approval_id, decision="reject", expected_signal_version=review.signal_version,
        confirm_text=f"REJECT {signal.signal_key}", quote=None,
        actor="reviewer", comment="Risk concern")
    assert approval.status == "rejected"
    assert approval.decision_at == NOW
    assert approval.decided_by == "reviewer"
    assert approval.comment == "Risk concern"
    assert order_id is None
    assert session.query(PaperOrders).count() == 0
    assert notifier.events[-1][0] == "trade_rejected"


def test_no_duplicate_review_stale_quote_invalid_quantity_or_stale_version(session, signal):
    manager = make_manager(session)
    review = manager.review(signal.signal_key, quote())
    with pytest.raises(ValueError, match="already has"):
        manager.review(signal.signal_key, quote())
    with pytest.raises(ValueError, match="stale"):
        manager.decide(review.approval_id, decision="approve", expected_signal_version=review.signal_version,
                       confirm_text=f"APPROVE {signal.signal_key}", quote=quote(NOW - timedelta(minutes=2)))
    with pytest.raises(ValueError, match="risk-qualified maximum"):
        manager.decide(review.approval_id, decision="approve", expected_signal_version=review.signal_version,
                       confirm_text=f"APPROVE {signal.signal_key}", quote=quote(), quantity=review.quantity + 1)
    signal.rationale = "Changed after review"
    session.flush()
    with pytest.raises(ValueError, match="changed since review"):
        manager.decide(review.approval_id, decision="approve", expected_signal_version=review.signal_version,
                       confirm_text=f"APPROVE {signal.signal_key}", quote=quote())
    assert session.query(PaperOrders).count() == 0


def test_kill_switch_and_expiry_block_approval_and_expiry_is_recorded(session, signal, tmp_path):
    notifier = MockNotifier()
    switch = KillSwitch(tmp_path / "kill.json")
    manager = make_manager(session, notifier, switch)
    review = manager.review(signal.signal_key, quote())
    switch.activate("test")
    with pytest.raises(PaperExecutionError, match="kill switch"):
        manager.decide(review.approval_id, decision="approve", expected_signal_version=review.signal_version,
                       confirm_text=f"APPROVE {signal.signal_key}", quote=quote())
    switch.deactivate("test reset")
    signal.expires_at = NOW - timedelta(seconds=1)
    session.flush()
    approval, order_id = manager.decide(
        review.approval_id, decision="approve", expected_signal_version=review.signal_version,
        confirm_text=f"APPROVE {signal.signal_key}", quote=quote())
    assert approval.status == "expired" and approval.decision_at == NOW
    assert order_id is None
    assert session.query(PaperOrders).count() == 0
    assert notifier.events[-1][0] == "signal_expired"


def test_telegram_notifier_is_outbound_only_and_logs_mocked_results(session):
    from config.settings import Settings
    from notifications.telegram import TelegramNotifier

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

    class FakeHttp:
        def __init__(self):
            self.calls = []

        def post(self, url, *, json, timeout):
            self.calls.append((url, json, timeout))
            return Response()

    settings = Settings(telegram_bot_token="test-secret", telegram_chat_id="123")
    http = FakeHttp()
    notifier = TelegramNotifier(session, settings=settings, http=http)
    record = notifier.notify("new_opportunity", "Review only")
    assert record.status == "sent"
    assert "test-secret" not in repr(settings)
    assert http.calls[0][0].endswith("/sendMessage")
    assert http.calls[0][1] == {"chat_id": "123", "text": "Review only"}
    assert "getUpdates" not in http.calls[0][0]
    with pytest.raises(ValueError, match="Unsupported"):
        notifier.notify("approve_trade", "must not exist")

    disabled_http = FakeHttp()
    disabled = TelegramNotifier(session, settings=Settings(_env_file=None), http=disabled_http)
    disabled_record = disabled.daily_report("Paper-only daily summary")
    assert disabled_record.status == "disabled"
    assert disabled_http.calls == []

    class FailedHttp(FakeHttp):
        def post(self, *args, **kwargs):
            raise RuntimeError("network unavailable")

    failed = TelegramNotifier(session, settings=settings, http=FailedHttp())
    failed_record = failed.notify("trade_rejected", "Rejected")
    assert failed_record.status == "failed"
    assert "test-secret" not in failed_record.message
