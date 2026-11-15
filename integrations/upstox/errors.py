"""Typed error handling for Upstox API and network failures."""

from dataclasses import dataclass
from typing import Any


class UpstoxError(Exception):
    """Base class for errors surfaced by the Upstox integration."""


class ConfigurationError(UpstoxError):
    """Required application configuration is missing or invalid."""


class AuthenticationError(UpstoxError):
    """OAuth state, authorization code, or token exchange is invalid."""


class TokenExpiredError(AuthenticationError):
    """The access token is absent, expired, or rejected by Upstox."""


class NetworkError(UpstoxError):
    """A network request failed before a valid API response was received."""


class InvalidResponseError(UpstoxError):
    """An API response did not match the documented response structure."""


@dataclass
class UpstoxAPIError(UpstoxError):
    """An HTTP/API error with safe structured metadata."""

    message: str
    status_code: int | None = None
    error_code: str | None = None
    retry_after_seconds: float | None = None
    response: Any = None

    def __str__(self) -> str:
        suffix = f" (HTTP {self.status_code})" if self.status_code is not None else ""
        code = f" [{self.error_code}]" if self.error_code else ""
        return f"{self.message}{suffix}{code}"
