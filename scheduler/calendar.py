"""Fail-closed NSE equity calendar with official-source import and special sessions."""

from __future__ import annotations

from csv import DictReader
from datetime import date, datetime, time, timezone
from io import StringIO
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from database.connection import transaction
from database.models import NSECalendarException, NSECalendarYear

INDIA_TZ = ZoneInfo("Asia/Kolkata")
NSE_HOLIDAY_PAGE = "https://www.nseindia.com/resources/exchange-communication-holidays"
NSE_2026_CIRCULAR = "https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf"

# NSE's published 2026 equity calendar, including the Maharashtra election
# closure listed on the official calendar page, and the separately announced
# Sunday Muhurat special session. Later years must be imported from NSE.
OFFICIAL_2026_EXCEPTIONS = (
    ("2026-01-15", "HOLIDAY", "Municipal Corporation Election - Maharashtra", None, None),
    ("2026-01-26", "HOLIDAY", "Republic Day", None, None),
    ("2026-03-03", "HOLIDAY", "Holi", None, None),
    ("2026-03-26", "HOLIDAY", "Shri Ram Navami", None, None),
    ("2026-03-31", "HOLIDAY", "Shri Mahavir Jayanti", None, None),
    ("2026-04-03", "HOLIDAY", "Good Friday", None, None),
    ("2026-04-14", "HOLIDAY", "Dr. Baba Saheb Ambedkar Jayanti", None, None),
    ("2026-05-01", "HOLIDAY", "Maharashtra Day", None, None),
    ("2026-05-28", "HOLIDAY", "Bakri Id", None, None),
    ("2026-06-26", "HOLIDAY", "Muharram", None, None),
    ("2026-09-14", "HOLIDAY", "Ganesh Chaturthi", None, None),
    ("2026-10-02", "HOLIDAY", "Mahatma Gandhi Jayanti", None, None),
    ("2026-10-20", "HOLIDAY", "Dussehra", None, None),
    ("2026-11-08", "SPECIAL_SESSION", "Diwali Laxmi Pujan - Muhurat Trading; timing per separate NSE circular", None, None),
    ("2026-11-10", "HOLIDAY", "Diwali-Balipratipada", None, None),
    ("2026-11-24", "HOLIDAY", "Prakash Gurpurb Sri Guru Nanak Dev", None, None),
    ("2026-12-25", "HOLIDAY", "Christmas", None, None),
)


def seed_official_2026_calendar(session: Session, *, now: datetime | None = None) -> bool:
    """Install the verified NSE 2026 calendar once; return whether it was inserted."""
    if session.get(NSECalendarYear, 2026) is not None:
        return False
    current = now or datetime.now(timezone.utc)
    with transaction(session):
        metadata = NSECalendarYear(year=2026, source_url=NSE_HOLIDAY_PAGE,
                                   source_name="NSE 2026 Equities calendar; circular CMTR/71775",
                                   source_published_at=None, refreshed_at=current)
        session.add(metadata)
        for raw_date, kind, description, opens_at, closes_at in OFFICIAL_2026_EXCEPTIONS:
            session.add(NSECalendarException(market_date=date.fromisoformat(raw_date), year=2026,
                                             kind=kind, description=description,
                                             opens_at=opens_at, closes_at=closes_at))
    return True


def import_official_calendar_csv(session: Session, csv_text: str, *, source_url: str,
                                 source_name: str, refreshed_at: datetime | None = None) -> int:
    """Replace one year from a user-supplied NSE calendar CSV.

    Required headers: date, kind, description. Optional special-session headers:
    opens_at, closes_at (HH:MM). Only official NSE hostnames are accepted.
    """
    hostname = (urlparse(source_url).hostname or "").lower()
    if not source_url.startswith("https://") or not (
            hostname == "nseindia.com" or hostname.endswith(".nseindia.com") or
            hostname == "nseindia.in" or hostname.endswith(".nseindia.in")):
        raise ValueError("Calendar source must be an HTTPS NSE India URL")
    if not source_name.strip() or not csv_text.strip():
        raise ValueError("Calendar CSV and source name are required")
    try:
        reader = DictReader(StringIO(csv_text.lstrip("\ufeff")))
        headers = {header.strip().lower() for header in (reader.fieldnames or [])}
        if not {"date", "kind", "description"} <= headers:
            raise ValueError("Calendar CSV requires date, kind, and description columns")
        rows: list[dict] = []
        dates: set[date] = set()
        year: int | None = None
        for line, raw in enumerate(reader, start=2):
            normalized = {str(key).strip().lower(): (value or "").strip()
                          for key, value in raw.items() if key}
            market_date = date.fromisoformat(normalized["date"])
            row_year = market_date.year
            if year is None:
                year = row_year
            if row_year != year:
                raise ValueError("A calendar CSV may contain dates from only one year")
            if market_date in dates:
                raise ValueError(f"Duplicate calendar date on row {line}")
            dates.add(market_date)
            kind = normalized["kind"].upper()
            if kind not in {"HOLIDAY", "SPECIAL_SESSION"}:
                raise ValueError(f"Invalid calendar kind on row {line}: {kind}")
            opens_at = time.fromisoformat(normalized["opens_at"]) if normalized.get("opens_at") else None
            closes_at = time.fromisoformat(normalized["closes_at"]) if normalized.get("closes_at") else None
            if (opens_at is None) != (closes_at is None):
                raise ValueError("Special-session opens_at and closes_at must both be supplied")
            if opens_at is not None and closes_at <= opens_at:
                raise ValueError("Special-session close must be later than its open")
            if kind == "HOLIDAY" and (opens_at is not None or closes_at is not None):
                raise ValueError("Holiday rows cannot include market session times")
            rows.append({"market_date": market_date, "year": row_year, "kind": kind,
                         "description": normalized["description"] or kind,
                         "opens_at": opens_at, "closes_at": closes_at})
    except (KeyError, TypeError) as exc:
        raise ValueError("Calendar CSV contains malformed rows") from exc
    if not rows or year is None:
        raise ValueError("Calendar CSV contains no calendar exceptions")
    timestamp = refreshed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("Calendar refresh time must be timezone-aware")
    with transaction(session):
        metadata = session.get(NSECalendarYear, year)
        if metadata is None:
            metadata = NSECalendarYear(year=year, source_url=source_url,
                                       source_name=source_name[:160], refreshed_at=timestamp)
            session.add(metadata)
        else:
            metadata.source_url = source_url
            metadata.source_name = source_name[:160]
            metadata.refreshed_at = timestamp
        session.execute(delete(NSECalendarException).where(NSECalendarException.year == year))
        session.flush()
        session.add_all(NSECalendarException(**row) for row in rows)
    return year


class NSETradingCalendar:
    """Calendar lookup; unknown years are never inferred as trading days."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def status(self, year: int) -> dict[str, object]:
        record = self.session.get(NSECalendarYear, year)
        exceptions = list(self.session.scalars(select(NSECalendarException).where(
            NSECalendarException.year == year).order_by(NSECalendarException.market_date)))
        return {
            "year": year, "verified": record is not None,
            "source_url": record.source_url if record else None,
            "source_name": record.source_name if record else None,
            "source_refreshed_at": record.refreshed_at if record else None,
            "exception_count": len(exceptions),
            "holidays": sum(item.kind == "HOLIDAY" for item in exceptions),
            "special_sessions": sum(item.kind == "SPECIAL_SESSION" for item in exceptions),
        }

    def exception(self, market_date: date) -> NSECalendarException | None:
        return self.session.get(NSECalendarException, market_date)

    def is_trading_day(self, market_date: date) -> bool:
        if self.session.get(NSECalendarYear, market_date.year) is None:
            return False
        override = self.exception(market_date)
        if override is not None:
            return override.kind == "SPECIAL_SESSION"
        return market_date.weekday() < 5

    def is_standard_session_day(self, market_date: date) -> bool:
        if not self.is_trading_day(market_date):
            return False
        override = self.exception(market_date)
        return override is None

    def session_bounds(self, market_date: date) -> tuple[datetime, datetime] | None:
        if not self.is_trading_day(market_date):
            return None
        override = self.exception(market_date)
        if override is not None:
            if override.kind != "SPECIAL_SESSION" or override.opens_at is None or override.closes_at is None:
                return None
            opening, closing = override.opens_at, override.closes_at
        else:
            opening, closing = time(9, 15), time(15, 30)
        return (datetime.combine(market_date, opening, INDIA_TZ),
                datetime.combine(market_date, closing, INDIA_TZ))

    def is_open_at(self, instant: datetime) -> bool:
        if instant.tzinfo is None:
            raise ValueError("Calendar query timestamp must be timezone-aware")
        india_time = instant.astimezone(INDIA_TZ)
        bounds = self.session_bounds(india_time.date())
        return bounds is not None and bounds[0] <= india_time <= bounds[1]
