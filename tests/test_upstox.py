"""Mocked unit tests for the Upstox read-only integration."""

from datetime import date, datetime, timedelta, timezone
import gzip
import json

import pytest
from pydantic import SecretStr

from config.settings import Settings
from integrations.upstox.authentication import OAuthAuthentication
from integrations.upstox.errors import (
    AuthenticationError, InvalidResponseError, NetworkError, TokenExpiredError, UpstoxAPIError,
)
from integrations.upstox.instrument_master import InstrumentMasterClient
from integrations.upstox.market_quotes import MarketQuoteClient
from integrations.upstox.historical_data import HistoricalCandleClient
from integrations.upstox.mock_adapter import MockUpstoxAdapter
from integrations.upstox.rate_limiter import SlidingWindowRateLimiter
from integrations.upstox.sdk_adapter import UpstoxSDKAdapter
from integrations.upstox.token_manager import TokenManager, documented_expiry
import upstox_client


def make_token_manager() -> TokenManager:
    manager = TokenManager(expiry_skew=timedelta(seconds=0))
    manager.set_token("test-token", expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    return manager


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        upstox_client_id=SecretStr("app-id"),
        upstox_client_secret=SecretStr("app-secret"),
        upstox_redirect_uri="http://localhost:8000/auth/callback",
    )


def successful_candles() -> dict:
    return {"status": "success", "data": {"candles": [
        ["2026-11-14T09:15:00+05:30", 100.0, 103.0, 99.0, 102.0, 1234, 0],
    ]}}


def test_documented_token_expiry_boundary_uses_ist() -> None:
    before_boundary = datetime(2026, 11, 14, 21, 0, tzinfo=timezone.utc)  # 02:30 IST
    after_boundary = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc)  # 03:30 IST
    assert documented_expiry(before_boundary) == after_boundary
    assert documented_expiry(after_boundary) == datetime(2026, 11, 15, 22, 0, tzinfo=timezone.utc)


def test_token_manager_rejects_missing_and_expired_tokens() -> None:
    manager = TokenManager(expiry_skew=timedelta(seconds=0))
    with pytest.raises(TokenExpiredError):
        manager.get_token()
    manager.set_token("secret", expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(TokenExpiredError):
        manager.get_token(now=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_oauth_builds_official_url_and_exchanges_only_after_state_validation() -> None:
    calls = []
    manager = TokenManager()
    auth = OAuthAuthentication(manager, settings=make_settings(), token_exchanger=lambda **kw: calls.append(kw) or {"access_token": "issued"})
    url, state = auth.authorization_url()
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog?")
    assert "response_type=code" in url
    assert "client_id=app-id" in url
    auth.handle_callback(code="one-use-code", state=state)
    assert calls[0]["client_id"] == "app-id"
    assert calls[0]["redirect_uri"] == "http://localhost:8000/auth/callback"
    assert manager.get_token() == "issued"
    with pytest.raises(AuthenticationError):
        auth.handle_callback(code="reused", state=state)


def test_oauth_rejects_bad_state_and_missing_access_token() -> None:
    def exchange(**_):
        return {"access_token": "issued"}

    auth = OAuthAuthentication(TokenManager(), settings=make_settings(), token_exchanger=exchange)
    with pytest.raises(AuthenticationError):
        auth.handle_callback(code="code", state="wrong-state")
    _, state = auth.authorization_url()
    auth.token_exchanger = lambda **_: {"user_id": "no-token"}
    with pytest.raises(AuthenticationError, match="access_token"):
        auth.handle_callback(code="code", state=state)


def test_oauth_denial_does_not_exchange_code() -> None:
    auth = OAuthAuthentication(TokenManager(), settings=make_settings(), token_exchanger=lambda **_: pytest.fail("must not exchange"))
    with pytest.raises(AuthenticationError, match="denied"):
        auth.handle_callback(code=None, state=None, error="access_denied")


def test_historical_candle_request_shape_provenance_and_cache() -> None:
    adapter = MockUpstoxAdapter(historical_candles=successful_candles())
    client = HistoricalCandleClient(adapter, make_token_manager(), limiter=SlidingWindowRateLimiter(((10, 1),)))
    first = client.historical("NSE_EQ|TESTKEY", "days", 1, date(2026, 11, 14), date(2026, 11, 1))
    second = client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-14", "2026-11-01")
    assert first.data_kind == "HISTORICAL" and not first.cache_hit
    assert first.retrieved_at.tzinfo is timezone.utc
    assert first.data[0][4] == 102.0
    assert second.cache_hit and second.retrieved_at == first.retrieved_at
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0] == "historical_candles"
    assert adapter.calls[0][1][1:] == ("NSE_EQ|TESTKEY", "days", 1, "2026-11-14", "2026-11-01")


def test_intraday_v3_route_is_distinguished_from_historical() -> None:
    adapter = MockUpstoxAdapter(intraday_candles=successful_candles())
    client = HistoricalCandleClient(adapter, make_token_manager(), limiter=SlidingWindowRateLimiter(((10, 1),)))
    result = client.intraday("NSE_EQ|TESTKEY", "minutes", 5)
    assert result.data_kind == "LIVE_INTRADAY"
    assert result.source == "Upstox Intraday Candle Data V3"
    assert adapter.calls[0][0] == "intraday_candles"
    assert adapter.calls[0][1][1:] == ("NSE_EQ|TESTKEY", "minutes", 5)


@pytest.mark.parametrize("unit,interval", [("minutes", 0), ("minutes", 301), ("hours", 6), ("days", 2), ("weeks", 1)])
def test_intraday_rejects_unsupported_intervals_before_api_call(unit: str, interval: int) -> None:
    adapter = MockUpstoxAdapter()
    client = HistoricalCandleClient(adapter, make_token_manager())
    with pytest.raises(ValueError):
        client.intraday("NSE_EQ|TESTKEY", unit, interval)
    assert adapter.calls == []


def test_historical_rejects_inverted_date_range() -> None:
    client = HistoricalCandleClient(MockUpstoxAdapter(), make_token_manager())
    with pytest.raises(ValueError, match="from_date"):
        client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-01", "2026-11-02")


def test_invalid_candle_response_is_rejected() -> None:
    adapter = MockUpstoxAdapter(historical_candles={"status": "success", "data": {"candles": [["bad", 1, 2]]}})
    client = HistoricalCandleClient(adapter, make_token_manager())
    with pytest.raises(InvalidResponseError):
        client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-14")


def test_expired_token_from_api_invalidates_manager_without_retry() -> None:
    adapter = MockUpstoxAdapter(market_quotes=UpstoxAPIError("expired", status_code=401))
    manager = make_token_manager()
    client = MarketQuoteClient(adapter, manager, limiter=SlidingWindowRateLimiter(((10, 1),)))
    with pytest.raises(TokenExpiredError):
        client.full_quotes(["NSE_EQ|TESTKEY"])
    assert len(adapter.calls) == 1
    with pytest.raises(TokenExpiredError):
        manager.get_token()


def test_market_quotes_are_validated_cached_and_limited_to_500() -> None:
    response = {"status": "success", "data": {"NSE_EQ:TEST": {
        "instrument_token": "NSE_EQ|TESTKEY", "last_price": 102.5,
        "ohlc": {"open": 100, "high": 104, "low": 99, "close": 101},
    }}}
    adapter = MockUpstoxAdapter(market_quotes=response)
    client = MarketQuoteClient(adapter, make_token_manager(), limiter=SlidingWindowRateLimiter(((10, 1),)))
    first = client.full_quotes(["NSE_EQ|TESTKEY"])
    cached = client.full_quotes(["NSE_EQ|TESTKEY"])
    assert first.data_kind == "LIVE_QUOTE" and first.data["NSE_EQ:TEST"]["last_price"] == 102.5
    assert cached.cache_hit and cached.retrieved_at == first.retrieved_at
    assert len(adapter.calls) == 1
    with pytest.raises(ValueError, match="1 and 500"):
        client.full_quotes([f"NSE_EQ|KEY{i}" for i in range(501)])


def test_invalid_quote_data_is_rejected() -> None:
    adapter = MockUpstoxAdapter(market_quotes={"status": "success", "data": {"NSE_EQ:X": {"last_price": "bad"}}})
    client = MarketQuoteClient(adapter, make_token_manager())
    with pytest.raises(InvalidResponseError):
        client.full_quotes(["NSE_EQ|X"])


def test_safe_read_retries_rate_limit_then_succeeds() -> None:
    response = {"status": "success", "data": {"candles": []}}
    adapter = MockUpstoxAdapter(historical_candles=[UpstoxAPIError("slow down", status_code=429, retry_after_seconds=0), response])
    client = HistoricalCandleClient(adapter, make_token_manager(), limiter=SlidingWindowRateLimiter(((10, 1),)))
    client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-14")
    assert len(adapter.calls) == 2


def test_safe_read_retries_network_failure_but_not_client_error() -> None:
    adapter = MockUpstoxAdapter(historical_candles=[NetworkError("offline"), successful_candles()])
    client = HistoricalCandleClient(adapter, make_token_manager(), limiter=SlidingWindowRateLimiter(((10, 1),)))
    client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-14")
    assert len(adapter.calls) == 2

    adapter = MockUpstoxAdapter(historical_candles=UpstoxAPIError("bad request", status_code=400))
    client = HistoricalCandleClient(adapter, make_token_manager())
    with pytest.raises(UpstoxAPIError):
        client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-14")
    assert len(adapter.calls) == 1


def test_safe_read_retries_timeout_with_a_bounded_attempt_count() -> None:
    adapter = MockUpstoxAdapter(historical_candles=[TimeoutError("timed out"), successful_candles()])
    client = HistoricalCandleClient(adapter, make_token_manager(),
                                    limiter=SlidingWindowRateLimiter(((10, 1),)))
    client.historical("NSE_EQ|TESTKEY", "days", 1, "2026-11-14")
    assert len(adapter.calls) == 2


def test_instrument_master_download_validation_and_cache(tmp_path) -> None:
    records = [{"instrument_key": "NSE_EQ|TESTKEY", "trading_symbol": "TEST", "name": "Test Ltd",
                "exchange": "NSE", "segment": "NSE_EQ", "instrument_type": "EQ"}]
    content = gzip.compress(json.dumps(records).encode("utf-8"))
    adapter = MockUpstoxAdapter(download_instruments=content)
    client = InstrumentMasterClient(cache_path=tmp_path / "nse.json.gz", downloader=adapter.download_instruments)
    first = client.get_nse_instruments()
    cached = client.get_nse_instruments()
    assert first.instruments[0].instrument_key == "NSE_EQ|TESTKEY"
    assert first.data_kind == "REFERENCE_MASTER" and not first.cache_hit
    assert cached.cache_hit and cached.retrieved_at == first.retrieved_at
    assert len(adapter.calls) == 1
    assert adapter.calls[0][2]["url"] == "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


def test_instrument_master_rejects_invalid_document(tmp_path) -> None:
    client = InstrumentMasterClient(cache_path=tmp_path / "bad.json.gz", downloader=lambda *_: b"not-json")
    with pytest.raises(InvalidResponseError):
        client.get_nse_instruments()


def test_rate_limiter_waits_at_window_boundary() -> None:
    now = [0.0]
    sleeps = []

    def sleep(duration: float) -> None:
        sleeps.append(duration)
        now[0] += duration

    limiter = SlidingWindowRateLimiter(((1, 1.0),), clock=lambda: now[0], sleep=sleep)
    limiter.acquire()
    limiter.acquire()
    assert sleeps == [1.0]


def test_official_sdk_adapter_sets_bearer_header_and_calls_v3_candle_method(monkeypatch) -> None:
    calls = []

    class Pool:
        def clear(self):
            calls.append(("clear",))

    class FakeApiClient:
        def __init__(self, configuration):
            self.configuration = configuration
            self.rest_client = type("Rest", (), {"pool_manager": Pool()})()

        def set_default_header(self, name, value):
            calls.append(("header", name, value))

    class FakeHistory:
        def __init__(self, api_client):
            self.api_client = api_client

        def get_historical_candle_data1_with_http_info(self, *args, **kwargs):
            calls.append(("historical", args, kwargs))
            return successful_candles(), 200, {}

    class FakeQuotes:
        def __init__(self, api_client):
            pass

    monkeypatch.setattr(upstox_client, "ApiClient", FakeApiClient)
    monkeypatch.setattr(upstox_client, "HistoryV3Api", FakeHistory)
    monkeypatch.setattr(upstox_client, "MarketQuoteV3Api", FakeQuotes)
    adapter = UpstoxSDKAdapter(timeout=4)
    result = adapter.historical_candles("sdk-token", "NSE_EQ|TESTKEY", "days", 1, "2026-11-14", "2026-11-01")
    assert result == successful_candles()
    assert ("header", "Authorization", "Bearer sdk-token") in calls
    assert calls[1][0] == "historical"
    assert calls[1][1] == ("NSE_EQ|TESTKEY", "days", 1, "2026-11-14", "2026-11-01")
    assert calls[1][2]["_request_timeout"] == 4
    assert calls[-1] == ("clear",)


def test_official_sdk_adapter_sends_quotes_as_comma_separated_parameter(monkeypatch) -> None:
    recorded = {}

    class FakeApiClient:
        def __init__(self, configuration): self.rest_client = type("Rest", (), {"pool_manager": type("Pool", (), {"clear": lambda self: None})()})()
        def set_default_header(self, name, value): recorded[name] = value

    class FakeHistory:
        def __init__(self, api_client): pass

    class FakeQuotes:
        def __init__(self, api_client): pass
        def get_full_market_quote_v3_with_http_info(self, **kwargs):
            recorded.update(kwargs)
            return {"status": "success", "data": {}}, 200, {}

    monkeypatch.setattr(upstox_client, "ApiClient", FakeApiClient)
    monkeypatch.setattr(upstox_client, "HistoryV3Api", FakeHistory)
    monkeypatch.setattr(upstox_client, "MarketQuoteV3Api", FakeQuotes)
    UpstoxSDKAdapter().market_quotes("sdk-token", ["NSE_EQ|A", "NSE_EQ|B"])
    assert recorded["instrument_key"] == "NSE_EQ|A,NSE_EQ|B"
    assert recorded["Authorization"] == "Bearer sdk-token"
