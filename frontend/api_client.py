"""Authenticated client for the separately hosted TradePilot API."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin

import requests
import streamlit as st

from config.settings import get_settings


class ApiUnavailable(RuntimeError):
    """Raised when the configured API cannot serve a request."""


def _secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
    except Exception:
        value = default
    return str(value or os.getenv(name, default))


def api_base_url() -> str:
    return _secret("API_BASE_URL", get_settings().api_base_url).rstrip("/")


def api_request(method: str, path: str, *, timeout: float | None = None,
                **kwargs: Any) -> requests.Response:
    base = api_base_url()
    if not base.startswith("https://") and get_settings().app_env.lower() in {"prod", "production"}:
        raise ApiUnavailable("The production API_BASE_URL must use HTTPS")
    token = _secret("API_AUTH_TOKEN", get_settings().api_auth_token.get_secret_value())
    if get_settings().app_env.lower() in {"prod", "production"} and not token:
        raise ApiUnavailable("API_AUTH_TOKEN is missing from Streamlit secrets")
    headers = dict(kwargs.pop("headers", {}) or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(method, urljoin(base + "/", path.lstrip("/")),
                                    headers=headers, timeout=timeout or get_settings().api_request_timeout_seconds,
                                    **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise ApiUnavailable(f"API request failed ({type(exc).__name__})") from exc


def api_health() -> tuple[bool, str]:
    try:
        response = requests.get(urljoin(api_base_url() + "/", "health"), timeout=3)
        if response.ok:
            return True, "Connected"
        return False, f"API returned HTTP {response.status_code}"
    except requests.RequestException as exc:
        return False, f"Unavailable ({type(exc).__name__})"


def require_frontend_access() -> bool:
    """Optional single-owner password gate; production fails closed if unset."""
    import hmac
    expected = _secret("APP_ACCESS_PASSWORD", get_settings().app_access_password.get_secret_value())
    production = get_settings().app_env.lower() in {"prod", "production"}
    if not expected:
        if production:
            st.error("Set APP_ACCESS_PASSWORD in Streamlit secrets before making this app public.")
            return False
        return True
    if st.session_state.get("tradepilot_authenticated"):
        return True
    supplied = st.text_input("App password", type="password")
    if st.button("Sign in", type="primary"):
        if hmac.compare_digest(supplied, expected):
            st.session_state["tradepilot_authenticated"] = True
            st.rerun()
        st.error("Incorrect password")
    return False
