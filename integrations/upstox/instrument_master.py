"""Download, validate, and cache Upstox's NSE JSON instrument master."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import random
import time
from typing import Any

import requests

from config.settings import PROJECT_ROOT, get_settings
from integrations.upstox.errors import InvalidResponseError, NetworkError, UpstoxAPIError

NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


@dataclass(frozen=True)
class InstrumentRecord:
    instrument_key: str
    trading_symbol: str
    name: str
    exchange: str
    segment: str
    instrument_type: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class InstrumentMasterSnapshot:
    instruments: tuple[InstrumentRecord, ...]
    retrieved_at: datetime
    source: str
    cache_hit: bool
    data_kind: str = "REFERENCE_MASTER"


def _download_nse_master(url: str = NSE_INSTRUMENTS_URL, timeout: float = 15.0) -> bytes:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=timeout, headers={"Accept": "application/json, application/gzip"})
            if response.status_code == 200:
                return response.content
            error = UpstoxAPIError("Upstox instrument master download failed", status_code=response.status_code)
            if response.status_code != 429 and not 500 <= response.status_code < 600:
                raise error
            last_error = error
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else min(3.0, 0.25 * (2 ** attempt) * (0.75 + random.random() * 0.5))
            except (TypeError, ValueError):
                delay = min(3.0, 0.25 * (2 ** attempt) * (0.75 + random.random() * 0.5))
        except requests.RequestException as exc:
            last_error = exc
            delay = min(3.0, 0.25 * (2 ** attempt) * (0.75 + random.random() * 0.5))
        if attempt < 2:
            time.sleep(max(0.0, min(delay, 3.0)))
    if isinstance(last_error, UpstoxAPIError):
        raise last_error
    raise NetworkError("Could not download the Upstox instrument master") from last_error


def _decode_document(content: bytes) -> Any:
    try:
        raw = gzip.decompress(content) if content[:2] == b"\x1f\x8b" else content
        return json.loads(raw.decode("utf-8"))
    except (gzip.BadGzipFile, EOFError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidResponseError("Instrument master was not valid gzip/JSON") from exc


def _validate_instruments(document: Any) -> tuple[InstrumentRecord, ...]:
    if not isinstance(document, list):
        raise InvalidResponseError("Instrument master must be a JSON array")
    instruments: list[InstrumentRecord] = []
    seen: set[str] = set()
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise InvalidResponseError(f"Instrument record {index} is not an object")
        required = ("instrument_key", "trading_symbol", "name", "exchange", "segment", "instrument_type")
        if any(not isinstance(item.get(key), str) or not item[key] for key in required):
            raise InvalidResponseError(f"Instrument record {index} is missing required documented string fields")
        key = item["instrument_key"]
        if key in seen:
            raise InvalidResponseError(f"Instrument master contains duplicate instrument_key: {key}")
        seen.add(key)
        instruments.append(InstrumentRecord(
            instrument_key=key,
            trading_symbol=item["trading_symbol"],
            name=item["name"],
            exchange=item["exchange"],
            segment=item["segment"],
            instrument_type=item["instrument_type"],
            raw=item,
        ))
    return tuple(instruments)


class InstrumentMasterClient:
    """Read-only NSE master client with a compressed on-disk public-data cache."""

    def __init__(
        self,
        *,
        cache_path: Path | str | None = None,
        cache_ttl: timedelta = timedelta(hours=12),
        downloader=_download_nse_master,
        timeout: float | None = None,
    ) -> None:
        if cache_ttl <= timedelta(0):
            raise ValueError("cache_ttl must be positive")
        settings = get_settings()
        self.cache_path = Path(cache_path or settings.upstox_instrument_cache_path)
        if not self.cache_path.is_absolute():
            self.cache_path = (PROJECT_ROOT / self.cache_path).resolve()
        self.cache_ttl = cache_ttl
        self.downloader = downloader
        self.timeout = timeout or settings.upstox_api_timeout_seconds

    def get_nse_instruments(self, *, force_refresh: bool = False) -> InstrumentMasterSnapshot:
        """Return validated NSE instruments, using a fresh local cache when possible."""
        now = datetime.now(timezone.utc)
        if not force_refresh and self.cache_path.exists():
            modified = datetime.fromtimestamp(self.cache_path.stat().st_mtime, timezone.utc)
            if now - modified <= self.cache_ttl:
                try:
                    instruments = _validate_instruments(_decode_document(self.cache_path.read_bytes()))
                    return InstrumentMasterSnapshot(instruments, modified, NSE_INSTRUMENTS_URL, True)
                except (OSError, InvalidResponseError):
                    # A corrupt/stale cache is replaced from the official source.
                    pass
        content = self.downloader(NSE_INSTRUMENTS_URL, self.timeout)
        instruments = _validate_instruments(_decode_document(content))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        compressed = content if content[:2] == b"\x1f\x8b" else gzip.compress(content)
        temporary = self.cache_path.with_name(self.cache_path.name + ".tmp")
        try:
            temporary.write_bytes(compressed)
            temporary.replace(self.cache_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise NetworkError("Unable to cache downloaded instrument master") from exc
        retrieved = datetime.fromtimestamp(self.cache_path.stat().st_mtime, timezone.utc)
        return InstrumentMasterSnapshot(instruments, retrieved, NSE_INSTRUMENTS_URL, False)
