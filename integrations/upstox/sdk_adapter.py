"""Official Upstox Python SDK adapter for read-only V3 market endpoints."""

from typing import Any

import upstox_client
from upstox_client.rest import ApiException as SDKApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from integrations.upstox.errors import NetworkError, TokenExpiredError, UpstoxAPIError, UpstoxError


def _to_mapping(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, tuple):
        return _to_mapping(value[0])
    return value


def _api_exception(exc: SDKApiException) -> UpstoxError:
    headers = getattr(exc, "headers", None) or {}
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    try:
        retry_after = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    body = getattr(exc, "body", None)
    message = "Upstox API request failed"
    error_code = None
    if body:
        try:
            import json
            decoded = json.loads(body) if isinstance(body, (str, bytes)) else body
            error_obj = decoded.get("errors", [{}])[0] if isinstance(decoded, dict) else {}
            message = error_obj.get("message") or decoded.get("message") or message
            error_code = error_obj.get("errorCode") or error_obj.get("error_code")
        except (ValueError, TypeError, AttributeError, IndexError):
            pass
    if exc.status == 401:
        return TokenExpiredError(message)
    return UpstoxAPIError(message, status_code=exc.status, error_code=error_code,
                          retry_after_seconds=retry_after, response=body)


class UpstoxSDKAdapter:
    """Uses SDK-generated methods for candle and full quote requests only."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def _apis(self, token: str) -> tuple[upstox_client.HistoryV3Api, upstox_client.MarketQuoteV3Api, upstox_client.ApiClient]:
        configuration = upstox_client.Configuration()
        configuration.host = "https://api.upstox.com"
        configuration.access_token = token
        api_client = upstox_client.ApiClient(configuration)
        # The generated history V3 methods currently omit auth_settings. Add the
        # documented Bearer header for every request, including those methods.
        api_client.set_default_header("Authorization", f"Bearer {token}")
        return upstox_client.HistoryV3Api(api_client), upstox_client.MarketQuoteV3Api(api_client), api_client

    @staticmethod
    def _release(api_client: upstox_client.ApiClient) -> None:
        """Release SDK urllib3 connections; generated ApiClient has no close method."""
        pool_manager = getattr(getattr(api_client, "rest_client", None), "pool_manager", None)
        if pool_manager is not None:
            pool_manager.clear()

    def _request(self, token: str, method_name: str, *args: Any, **kwargs: Any) -> Any:
        history, quotes, api_client = self._apis(token)
        owner = quotes if method_name == "get_full_market_quote_v3_with_http_info" else history
        try:
            method = getattr(owner, method_name)
            result = method(*args, _request_timeout=self.timeout, **kwargs)
            return _to_mapping(result)
        except SDKApiException as exc:
            raise _api_exception(exc) from exc
        except (TimeoutError, OSError, Urllib3HTTPError) as exc:
            raise NetworkError("Network failure while calling Upstox market data API") from exc
        finally:
            self._release(api_client)

    def historical_candles(self, token: str, instrument_key: str, unit: str, interval: int,
                           to_date: str, from_date: str | None) -> Any:
        if from_date is not None:
            return self._request(token, "get_historical_candle_data1_with_http_info",
                                 instrument_key, unit, interval, to_date, from_date)
        return self._request(token, "get_historical_candle_data_with_http_info",
                             instrument_key, unit, interval, to_date)

    def intraday_candles(self, token: str, instrument_key: str, unit: str, interval: int) -> Any:
        return self._request(token, "get_intra_day_candle_data_with_http_info", instrument_key, unit, interval)

    def market_quotes(self, token: str, instrument_keys: list[str]) -> Any:
        return self._request(token, "get_full_market_quote_v3_with_http_info",
                             instrument_key=",".join(instrument_keys))
