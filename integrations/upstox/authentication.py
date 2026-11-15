"""Documented Upstox OAuth 2.0 authorization-code flow."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import secrets
from threading import RLock
from typing import Any
from urllib.parse import urlencode

import upstox_client
from upstox_client.rest import ApiException as SDKApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from config.settings import Settings, get_settings
from integrations.upstox.errors import AuthenticationError, UpstoxAPIError
from integrations.upstox.token_manager import TokenManager

AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_PATH = "/v2/login/authorization/token"
TOKEN_API_VERSION = "2.0"


def _plain_response(value: Any) -> dict[str, Any]:
    if isinstance(value, tuple):
        value = value[0]
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise AuthenticationError("Upstox token response was not a JSON object")
    return value


def sdk_exchange_code(
    *, code: str, client_id: str, client_secret: str, redirect_uri: str,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Exchange the one-use authorization code with the official Upstox SDK."""
    configuration = upstox_client.Configuration()
    configuration.host = "https://api.upstox.com"
    api_client = upstox_client.ApiClient(configuration)
    try:
        result = upstox_client.LoginApi(api_client).token_with_http_info(
            api_version=TOKEN_API_VERSION,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            grant_type="authorization_code",
            _request_timeout=timeout,
        )
        return _plain_response(result)
    except SDKApiException as exc:
        raise UpstoxAPIError("Upstox OAuth token exchange failed", status_code=exc.status,
                             response=getattr(exc, "body", None)) from exc
    except (TimeoutError, OSError, Urllib3HTTPError) as exc:
        raise AuthenticationError("Network failure during Upstox OAuth token exchange") from exc
    finally:
        pool_manager = getattr(getattr(api_client, "rest_client", None), "pool_manager", None)
        if pool_manager is not None:
            pool_manager.clear()


class OAuthAuthentication:
    """Creates authorization URLs and validates callback state before exchange."""

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        settings: Settings | None = None,
        token_exchanger: Callable[..., dict[str, Any]] = sdk_exchange_code,
        state_ttl: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings or get_settings()
        self.token_manager = token_manager
        self.token_exchanger = token_exchanger
        self.state_ttl = state_ttl
        self.clock = clock
        self._states: dict[str, datetime] = {}
        self._lock = RLock()

    def authorization_url(self) -> tuple[str, str]:
        """Return the Upstox OAuth login URL and the state value to retain server-side."""
        client_id = self.settings.upstox_client_id.get_secret_value()
        if not client_id:
            raise AuthenticationError("UPSTOX_CLIENT_ID is not configured")
        if not self.settings.upstox_redirect_uri:
            raise AuthenticationError("UPSTOX_REDIRECT_URI is not configured")
        state = secrets.token_urlsafe(32)
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("OAuth clock must return timezone-aware datetimes")
        with self._lock:
            cutoff = now.astimezone(timezone.utc) - self.state_ttl
            self._states = {key: issued for key, issued in self._states.items() if issued >= cutoff}
            self._states[state] = now.astimezone(timezone.utc)
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": self.settings.upstox_redirect_uri,
            "response_type": "code",
            "state": state,
        })
        return f"{AUTHORIZE_URL}?{params}", state

    def handle_callback(self, *, code: str | None, state: str | None, error: str | None = None) -> None:
        """Validate OAuth callback state and exchange the single-use code for a token."""
        if error:
            raise AuthenticationError(f"Upstox authorization was denied: {error}")
        if not code or not state:
            raise AuthenticationError("OAuth callback must include both code and state")
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("OAuth clock must return timezone-aware datetimes")
        with self._lock:
            issued = self._states.pop(state, None)
        if issued is None or now.astimezone(timezone.utc) - issued > self.state_ttl:
            raise AuthenticationError("OAuth state is invalid, expired, or already used")

        client_id = self.settings.upstox_client_id.get_secret_value()
        client_secret = self.settings.upstox_client_secret.get_secret_value()
        if not client_id or not client_secret:
            raise AuthenticationError("UPSTOX_CLIENT_ID and UPSTOX_CLIENT_SECRET are required")
        try:
            payload = self.token_exchanger(
                code=code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=self.settings.upstox_redirect_uri,
                timeout=self.settings.upstox_api_timeout_seconds,
            )
        except UpstoxAPIError as exc:
            if exc.status_code == 401:
                raise AuthenticationError("Upstox rejected the authorization code or credentials") from exc
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise AuthenticationError("Upstox token response is missing access_token")
        self.token_manager.set_token(payload["access_token"], issued_at=now)
