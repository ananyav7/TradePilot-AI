"""Backend OAuth route tests without contacting Upstox."""

import pytest
from datetime import datetime, timedelta, timezone

from integrations.upstox import routes


def test_login_redirects_to_authorization_url(monkeypatch) -> None:
    expected = "https://api.upstox.com/v2/login/authorization/dialog?state=test"
    monkeypatch.setattr(
        routes.upstox_authentication,
        "authorization_url",
        lambda: (expected, "test"),
    )

    response = routes.start_upstox_login()

    assert response.status_code == 302
    assert response.headers["location"] == expected


def test_callback_stores_token_but_never_returns_it(monkeypatch) -> None:
    token_value = "sensitive-access-token"

    def exchange(*, code: str | None, state: str | None, error: str | None = None) -> None:
        assert (code, state, error) == ("auth-code", "valid-state", None)
        routes.upstox_token_manager.set_token(token_value, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    monkeypatch.setattr(routes.upstox_authentication, "handle_callback", exchange)
    response = routes.upstox_oauth_callback(code="auth-code", state="valid-state")

    assert response["status"] == "authorized"
    assert token_value not in str(response)
    assert routes.upstox_token_manager.get_token() == token_value
    routes.upstox_token_manager.invalidate()


def test_callback_rejects_invalid_state(monkeypatch) -> None:
    def reject(*, code: str | None, state: str | None, error: str | None = None) -> None:
        raise routes.AuthenticationError("OAuth state mismatch")

    monkeypatch.setattr(routes.upstox_authentication, "handle_callback", reject)
    with pytest.raises(routes.HTTPException) as error:
        routes.upstox_oauth_callback(code="auth-code", state="wrong")

    assert error.value.status_code == 400
    assert "state mismatch" in error.value.detail
