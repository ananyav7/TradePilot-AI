"""Backend-only OAuth routes. Secrets and bearer tokens are never returned."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from integrations.upstox.authentication import OAuthAuthentication
from integrations.upstox.client import MarketDataClient
from integrations.upstox.errors import AuthenticationError, UpstoxError
from integrations.upstox.token_manager import TokenManager

router = APIRouter(tags=["upstox-auth"])
upstox_token_manager = TokenManager()
upstox_authentication = OAuthAuthentication(upstox_token_manager)
upstox_market_data = MarketDataClient(upstox_token_manager)


@router.get("/auth/upstox/login", include_in_schema=True)
def start_upstox_login() -> RedirectResponse:
    """Redirect the user's browser to the official Upstox authorization dialog."""
    try:
        authorization_url, _ = upstox_authentication.authorization_url()
    except AuthenticationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/auth/callback", include_in_schema=False)
def upstox_oauth_callback(code: str | None = None, state: str | None = None,
                          error: str | None = None) -> dict[str, str]:
    """Validate Upstox OAuth state and store the exchanged token in process memory."""
    try:
        upstox_authentication.handle_callback(code=code, state=state, error=error)
    except AuthenticationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UpstoxError as exc:
        # Keep provider bodies and credentials out of callback responses.
        raise HTTPException(status_code=502, detail="Upstox token exchange failed") from exc
    return {
        "status": "authorized",
        "token_storage": "in-memory only",
        "expires_at": upstox_token_manager.expires_at.isoformat() if upstox_token_manager.expires_at else "unknown",
        "message": "Upstox read access is ready. No order endpoints are enabled.",
    }
