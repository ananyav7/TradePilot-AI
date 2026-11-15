"""Outbound-only Telegram Bot API alerts; this module never reads bot updates."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

import requests
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from database.connection import transaction
from database.models import NotificationLogs


class HttpPoster(Protocol):
    def post(self, url: str, *, json: dict, timeout: float): ...


class TelegramNotifier:
    """Send and persist alert-only messages. No approve/execute commands exist."""

    EVENTS = {"new_opportunity", "signal_expired", "approval_received", "trade_rejected",
              "paper_order_completed", "stop_loss_triggered", "target_reached", "daily_report"}

    def __init__(self, session: Session | None = None, *, settings: Settings | None = None,
                 http: HttpPoster | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.http = http or requests

    def notify(self, event: str, message: str) -> NotificationLogs:
        if event not in self.EVENTS:
            raise ValueError("Unsupported Telegram alert event")
        if not message.strip():
            raise ValueError("Notification message cannot be empty")
        token = self.settings.telegram_bot_token.get_secret_value()
        recipient = self.settings.telegram_chat_id
        status, sent_at = "disabled", None
        if token and recipient:
            try:
                response = self.http.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": recipient, "text": message[:4096]}, timeout=10.0)
                response.raise_for_status()
                payload = response.json()
                if payload.get("ok") is not True:
                    raise RuntimeError("Telegram API rejected the message")
                status, sent_at = "sent", datetime.now(timezone.utc)
            except Exception:
                # A notification provider is a best-effort side channel. It must
                # never undo a persisted paper decision or fill.
                status = "failed"
        record = NotificationLogs(channel="telegram", recipient=recipient or None,
                                  status=status, message=f"[{event}] {message[:4096]}", sent_at=sent_at)
        if self.session is not None:
            try:
                with transaction(self.session):
                    self.session.add(record)
                    self.session.flush()
            except Exception:
                self.session.rollback()
                record.status = "failed"
        return record

    def daily_report(self, text: str) -> NotificationLogs:
        """Send a caller-prepared daily paper-trading summary; no trades are triggered."""
        return self.notify("daily_report", text)
