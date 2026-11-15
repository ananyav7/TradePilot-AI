"""Backend endpoints for the read-only daily stock scanner."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.connection import get_db
from integrations.upstox.errors import TokenExpiredError
from integrations.upstox.routes import upstox_market_data, upstox_token_manager
from market_data.universe import StockUniverseService
from strategy.scanner import StockScanner

router = APIRouter(prefix="/universe", tags=["nifty-universe"])


@router.get("/scanner")
def scan_eligible_stocks(session: Session = Depends(get_db)) -> dict[str, object]:
    """Fetch and evaluate validated daily candles for enabled, mapped NSE equities."""
    try:
        upstox_token_manager.get_token()
    except TokenExpiredError as exc:
        raise HTTPException(status_code=401, detail="Complete Upstox authorization before running the scanner") from exc
    service = StockUniverseService(session)
    stocks = service.list_stocks(eligible_only=True)
    results = StockScanner(upstox_market_data).scan(stocks)
    return {"source": "Upstox Historical Candle Data V3", "universe_count": len(stocks),
            "results": [asdict(result) for result in results]}
