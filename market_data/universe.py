"""NSE NIFTY 50 constituent management and dynamic Upstox instrument resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
from io import StringIO
from pathlib import Path
import re
from typing import Callable, Iterable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.connection import transaction
from database.models import Stock, WatchlistStocks, Watchlists
from integrations.upstox.errors import InvalidResponseError, NetworkError
from integrations.upstox.instrument_master import InstrumentMasterClient, InstrumentMasterSnapshot, InstrumentRecord

NIFTY50_CSV_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
NIFTY50_SOURCE_PAGE = "https://www.nseindia.com/static/products-services/indices-nifty50-index"


@dataclass(frozen=True)
class Constituent:
    symbol: str
    name: str


@dataclass(frozen=True)
class UniverseSyncResult:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]
    unavailable: tuple[str, ...]
    source: str
    updated_at: datetime


@dataclass(frozen=True)
class UniverseStatus:
    source: str | None
    updated_at: datetime | None
    constituent_count: int
    enabled_count: int
    unavailable_symbols: tuple[str, ...]
    stale: bool


def _header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def parse_constituents_csv(text: str) -> tuple[Constituent, ...]:
    """Parse NSE's downloadable CSV or a user CSV with symbol/company columns."""
    if not text.strip():
        raise ValueError("Constituent CSV is empty")
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("Constituent CSV must include a header row")
    headers = {_header(name): name for name in reader.fieldnames if name}
    symbol_col = next((headers[name] for name in ("symbol", "tradingsymbol", "nsesymbol") if name in headers), None)
    name_col = next((headers[name] for name in ("companyname", "company", "name") if name in headers), None)
    if symbol_col is None:
        raise ValueError("Constituent CSV must contain a SYMBOL column")
    constituents: list[Constituent] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        symbol = (row.get(symbol_col) or "").strip().upper()
        if not symbol:
            continue
        if not re.fullmatch(r"[A-Z0-9&_-]{1,32}", symbol):
            raise ValueError(f"Invalid NSE symbol on CSV row {line}: {symbol!r}")
        if symbol in seen:
            raise ValueError(f"Duplicate NSE symbol in CSV: {symbol}")
        seen.add(symbol)
        name = (row.get(name_col) or symbol).strip() if name_col else symbol
        constituents.append(Constituent(symbol, name or symbol))
    if not constituents:
        raise ValueError("Constituent CSV does not contain any symbols")
    return tuple(constituents)


def download_official_constituents(*, timeout: float = 15.0,
                                   getter: Callable[..., requests.Response] = requests.get) -> tuple[str, str]:
    """Download the current official NSE CSV and reject content that is not its expected list."""
    try:
        response = getter(NIFTY50_CSV_URL, timeout=timeout, headers={"User-Agent": "TradePilotAI/1.0", "Accept": "text/csv,*/*"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NetworkError("Unable to retrieve the official NSE NIFTY 50 constituent CSV") from exc
    try:
        rows = parse_constituents_csv(response.content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidResponseError("NSE constituent download did not match the expected CSV schema") from exc
    if len(rows) != 50:
        raise InvalidResponseError(f"Official NIFTY 50 constituent list must contain exactly 50 symbols; got {len(rows)}")
    return response.content.decode("utf-8-sig"), NIFTY50_CSV_URL


def _equity_instrument_map(records: Iterable[InstrumentRecord]) -> dict[str, InstrumentRecord | None]:
    matches: dict[str, list[InstrumentRecord]] = {}
    for record in records:
        if (record.exchange == "NSE" and record.segment == "NSE_EQ" and record.instrument_type == "EQ"
                and record.instrument_key.startswith("NSE_EQ|")):
            matches.setdefault(record.trading_symbol.upper(), []).append(record)
    return {symbol: rows[0] if len(rows) == 1 else None for symbol, rows in matches.items()}


class StockUniverseService:
    """Transactional NIFTY membership, stock enablement, and user watchlist service."""

    def __init__(self, session: Session, *, instrument_master: InstrumentMasterClient | None = None) -> None:
        self.session = session
        self.instrument_master = instrument_master or InstrumentMasterClient()

    def refresh_official(self, *, force_master_refresh: bool = True) -> UniverseSyncResult:
        csv_text, source = download_official_constituents()
        return self.import_csv_text(csv_text, source=source, require_fifty=True,
                                    force_master_refresh=force_master_refresh)

    def import_csv(self, path: Path | str, *, source: str | None = None) -> UniverseSyncResult:
        file_path = Path(path)
        try:
            content = file_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"Unable to read constituent CSV: {file_path}") from exc
        return self.import_csv_text(content, source=source or f"CSV import: {file_path.name}", require_fifty=True)

    def import_csv_text(self, content: str, *, source: str = "CSV import", require_fifty: bool = True,
                        force_master_refresh: bool = False, master_snapshot: InstrumentMasterSnapshot | None = None
                        ) -> UniverseSyncResult:
        constituents = parse_constituents_csv(content)
        if require_fifty and len(constituents) != 50:
            raise ValueError(f"NIFTY 50 import requires exactly 50 constituents; got {len(constituents)}")
        snapshot = master_snapshot or self.instrument_master.get_nse_instruments(force_refresh=force_master_refresh)
        mapped = _equity_instrument_map(snapshot.instruments)
        now = datetime.now(timezone.utc)
        symbols = {item.symbol for item in constituents}
        existing = list(self.session.scalars(select(Stock).where(Stock.exchange == "NSE")))
        by_symbol = {stock.symbol.upper(): stock for stock in existing}
        previous_members = {stock.symbol.upper() for stock in existing if stock.in_nifty50}
        added, removed, unchanged, unavailable = [], [], [], []
        with transaction(self.session):
            for item in constituents:
                instrument = mapped.get(item.symbol)
                stock = by_symbol.get(item.symbol)
                if stock is None:
                    stock = Stock(symbol=item.symbol, exchange="NSE", name=item.name)
                    self.session.add(stock)
                    by_symbol[item.symbol] = stock
                if item.symbol not in previous_members:
                    added.append(item.symbol)
                else:
                    unchanged.append(item.symbol)
                stock.name = instrument.name if instrument else item.name
                stock.in_nifty50 = True
                stock.active = True
                stock.membership_source = source
                stock.membership_updated_at = now
                stock.instrument_key = instrument.instrument_key if instrument else None
                stock.instrument_available = instrument is not None
                if instrument is None:
                    unavailable.append(item.symbol)
            for symbol in previous_members - symbols:
                old_stock = by_symbol[symbol]
                old_stock.in_nifty50 = False
                old_stock.membership_source = source
                old_stock.membership_updated_at = now
                removed.append(symbol)
        return UniverseSyncResult(tuple(sorted(added)), tuple(sorted(removed)), tuple(sorted(unchanged)),
                                  tuple(sorted(unavailable)), source, now)

    def add_manual(self, symbol: str, *, watchlist_name: str = "Manual watchlist",
                   master_snapshot: InstrumentMasterSnapshot | None = None) -> Stock:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("NSE symbol is required")
        snapshot = master_snapshot or self.instrument_master.get_nse_instruments()
        mapped = _equity_instrument_map(snapshot.instruments).get(normalized)
        if mapped is None:
            raise ValueError(f"{normalized} is unavailable or ambiguous in the Upstox NSE equity instrument master")
        with transaction(self.session):
            stock = self.session.scalar(select(Stock).where(Stock.exchange == "NSE", Stock.symbol == normalized))
            if stock is None:
                stock = Stock(symbol=normalized, exchange="NSE", name=mapped.name)
                self.session.add(stock)
                self.session.flush()
            stock.name = mapped.name
            stock.instrument_key = mapped.instrument_key
            stock.instrument_available = True
            stock.active = True
            watchlist = self.session.scalar(select(Watchlists).where(Watchlists.name == watchlist_name))
            if watchlist is None:
                watchlist = Watchlists(name=watchlist_name, description="Manually added NSE equity symbols")
                self.session.add(watchlist)
                self.session.flush()
            membership = self.session.get(WatchlistStocks, (watchlist.id, stock.id))
            if membership is None:
                self.session.add(WatchlistStocks(watchlist_id=watchlist.id, stock_id=stock.id))
        return stock

    def set_enabled(self, symbol: str, enabled: bool) -> Stock:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        stock = self.session.scalar(select(Stock).where(Stock.exchange == "NSE", Stock.symbol == symbol.strip().upper()))
        if stock is None:
            raise LookupError(f"Unknown NSE symbol: {symbol}")
        if enabled and not stock.instrument_available:
            raise ValueError(f"{stock.symbol} cannot be enabled because its Upstox instrument mapping is unavailable")
        with transaction(self.session):
            stock.active = enabled
        return stock

    def list_stocks(self, *, eligible_only: bool = False) -> list[Stock]:
        statement = select(Stock).where(Stock.exchange == "NSE")
        if eligible_only:
            statement = statement.where(Stock.active.is_(True), Stock.instrument_available.is_(True),
                                        Stock.instrument_key.is_not(None))
        return list(self.session.scalars(statement.order_by(Stock.symbol)))

    def status(self) -> UniverseStatus:
        constituents = list(self.session.scalars(select(Stock).where(Stock.exchange == "NSE", Stock.in_nifty50.is_(True))))
        enabled = [stock for stock in constituents if stock.active]
        times = [stock.membership_updated_at for stock in constituents if stock.membership_updated_at]
        sources = [stock.membership_source for stock in constituents if stock.membership_source]
        updated_at = max(times) if times else None
        stale = updated_at is None or (datetime.now(timezone.utc) - updated_at).total_seconds() > 7 * 24 * 60 * 60
        return UniverseStatus(sources[0] if sources else None, updated_at, len(constituents), len(enabled),
                              tuple(sorted(stock.symbol for stock in constituents if not stock.instrument_available)), stale)

    def export_csv(self) -> str:
        rows = [stock for stock in self.list_stocks() if stock.in_nifty50]
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["symbol", "company_name", "exchange", "instrument_key", "instrument_available", "in_nifty50", "enabled", "source", "updated_at"])
        for stock in rows:
            writer.writerow([stock.symbol, stock.name, stock.exchange, stock.instrument_key or "",
                             stock.instrument_available, stock.in_nifty50, stock.active,
                             stock.membership_source or "", stock.membership_updated_at.isoformat() if stock.membership_updated_at else ""])
        return output.getvalue()
