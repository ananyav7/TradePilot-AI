"""Explicit backend endpoint for generating and persisting strategy research signals."""

from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Stock
from integrations.upstox.errors import TokenExpiredError, UpstoxError
from integrations.upstox.routes import upstox_market_data, upstox_token_manager
from strategy.scanner import candles_to_frame
from strategy.swing_strategy import SwingStrategyEngine
from strategy.technical_analysis import TechnicalAnalysisEngine

router = APIRouter(prefix="/strategy", tags=["swing-strategy"])
INDIA_TZ = ZoneInfo("Asia/Kolkata")


@router.post("/signals/generate")
def generate_strategy_signals(
    symbol: str | None = Query(default=None, min_length=1),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Fetch completed daily candles and persist one experimental signal per stock/date."""
    try:
        upstox_token_manager.get_token()
    except TokenExpiredError as exc:
        raise HTTPException(status_code=401, detail="Complete Upstox authorization before generating signals") from exc
    statement = select(Stock).where(Stock.exchange == "NSE", Stock.active.is_(True),
                                   Stock.instrument_available.is_(True), Stock.instrument_key.is_not(None))
    if symbol:
        statement = statement.where(Stock.symbol == symbol.strip().upper())
    stocks = list(session.scalars(statement.order_by(Stock.symbol)))
    if symbol and not stocks:
        raise HTTPException(status_code=404, detail=f"No enabled, mapped NSE stock found for {symbol.upper()}")
    if not stocks:
        raise HTTPException(status_code=409, detail="No enabled stocks with validated Upstox mappings are available")
    end = datetime.now(INDIA_TZ).date()
    start = end - timedelta(days=180)
    analysis_engine = TechnicalAnalysisEngine()
    strategy_engine = SwingStrategyEngine()
    generated = []
    skipped = []
    for stock in stocks:
        try:
            snapshot = upstox_market_data.historical_candles(stock.instrument_key, "days", 1, end, start)
            candle_frame = candles_to_frame(snapshot.data).set_index("timestamp")
            analysis = analysis_engine.analyze(candle_frame, end_of_day=True,
                                              source=snapshot.source)
            signal = strategy_engine.generate_and_store(session, stock, analysis)
            generated.append(asdict(signal))
        except (UpstoxError, ValueError, TypeError, KeyError, OSError, TimeoutError) as exc:
            skipped.append({"symbol": stock.symbol, "reason": type(exc).__name__})
    return {"strategy": strategy_engine.config.name, "version": strategy_engine.config.version,
            "source": "Upstox Historical Candle Data V3", "generated_count": len(generated),
            "skipped_count": len(skipped), "signals": generated, "skipped": skipped}
