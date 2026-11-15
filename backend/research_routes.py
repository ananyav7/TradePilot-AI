"""Read-only research explanation endpoints; these routes cannot approve or trade."""

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import TradeSignals
from strategy.swing_strategy import SwingStrategyEngine
from research.service import MarketResearchService

router = APIRouter(prefix="/research", tags=["market-research"])


@router.get("/signals/{signal_id}")
def explain_signal(signal_id: str, session: Session = Depends(get_db)) -> dict:
    """Return deterministic signal facts, sourced evidence, and optional AI interpretation."""
    record = session.scalar(select(TradeSignals).where(TradeSignals.signal_key == signal_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Signal was not found")
    signal = SwingStrategyEngine._from_record(record)
    report = MarketResearchService().explain_signal(signal, record.stock.name)
    return asdict(report)


@router.get("/daily-summary")
def daily_summary(market_date: date = Query(...), session: Session = Depends(get_db)) -> dict:
    """Summarize stored signals for one market date and retrieved market news."""
    records = session.scalars(select(TradeSignals).where(TradeSignals.signal_date == market_date)).all()
    signals = [SwingStrategyEngine._from_record(record) for record in records]
    return asdict(MarketResearchService().daily_summary(market_date, signals))
