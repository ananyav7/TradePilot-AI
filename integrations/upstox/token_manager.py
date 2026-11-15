"""In-memory token lifecycle. Tokens are never written to SQLite or disk."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from zoneinfo import ZoneInfo

from integrations.upstox.errors import TokenExpiredError

INDIA_TZ = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class AccessToken:
    """OAuth access token and its documented Upstox daily expiry time."""

    value: str
    issued_at: datetime
    expires_at: datetime


def documented_expiry(issued_at: datetime) -> datetime:
    """Upstox access tokens expire at 03:30 IST on the next upcoming boundary."""
    if issued_at.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")
    local = issued_at.astimezone(INDIA_TZ)
    expiry = local.replace(hour=3, minute=30, second=0, microsecond=0)
    if local >= expiry:
        expiry += timedelta(days=1)
    return expiry.astimezone(timezone.utc)


class TokenManager:
    """Thread-safe ephemeral token store with expiry skew and invalidation."""

    def __init__(self, *, expiry_skew: timedelta = timedelta(seconds=60)) -> None:
        if expiry_skew < timedelta(0):
            raise ValueError("expiry_skew cannot be negative")
        self._expiry_skew = expiry_skew
        self._token: AccessToken | None = None
        self._lock = RLock()

    def set_token(self, value: str, *, issued_at: datetime | None = None, expires_at: datetime | None = None) -> AccessToken:
        if not value or not value.strip():
            raise ValueError("Access token cannot be empty")
        issued = issued_at or datetime.now(timezone.utc)
        if issued.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware")
        expiry = expires_at or documented_expiry(issued)
        if expiry.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        token = AccessToken(value=value.strip(), issued_at=issued.astimezone(timezone.utc),
                            expires_at=expiry.astimezone(timezone.utc))
        with self._lock:
            self._token = token
        return token

    def get_token(self, *, now: datetime | None = None) -> str:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        with self._lock:
            if self._token is None:
                raise TokenExpiredError("No Upstox access token is available; complete OAuth authorization")
            if current.astimezone(timezone.utc) + self._expiry_skew >= self._token.expires_at:
                self._token = None
                raise TokenExpiredError("Upstox access token has expired; complete OAuth authorization again")
            return self._token.value

    def invalidate(self) -> None:
        """Forget a token after Upstox rejects it."""
        with self._lock:
            self._token = None

    @property
    def expires_at(self) -> datetime | None:
        with self._lock:
            return self._token.expires_at if self._token else None


class SharedTokenManager(TokenManager):
    """Encrypt the Upstox token in shared PostgreSQL for the isolated worker."""

    provider = "upstox"

    def _cipher(self):
        from config.settings import get_settings
        key = get_settings().upstox_token_encryption_key.get_secret_value()
        if not key:
            return None
        from cryptography.fernet import Fernet
        return Fernet(key.encode("ascii"))

    def _restore(self) -> None:
        cipher = self._cipher()
        if cipher is None:
            return
        from database.connection import SessionLocal
        from database.models import IntegrationCredentials
        try:
            with SessionLocal() as session:
                record = session.get(IntegrationCredentials, self.provider)
                if record:
                    value = cipher.decrypt(record.encrypted_token.encode("ascii")).decode("utf-8")
                    super().set_token(value, issued_at=record.issued_at, expires_at=record.expires_at)
        except Exception as exc:
            raise TokenExpiredError("Stored Upstox authorization is unavailable; authorize again") from exc

    def set_token(self, value: str, *, issued_at: datetime | None = None,
                  expires_at: datetime | None = None) -> AccessToken:
        token = super().set_token(value, issued_at=issued_at, expires_at=expires_at)
        cipher = self._cipher()
        if cipher is not None:
            from database.connection import SessionLocal
            from database.models import IntegrationCredentials
            with SessionLocal() as session:
                record = session.get(IntegrationCredentials, self.provider)
                if record is None:
                    record = IntegrationCredentials(provider=self.provider,
                        encrypted_token=cipher.encrypt(token.value.encode("utf-8")).decode("ascii"),
                        issued_at=token.issued_at, expires_at=token.expires_at)
                    session.add(record)
                else:
                    record.encrypted_token = cipher.encrypt(token.value.encode("utf-8")).decode("ascii")
                    record.issued_at, record.expires_at = token.issued_at, token.expires_at
                session.commit()
        return token

    def get_token(self, *, now: datetime | None = None) -> str:
        if self._token is None:
            self._restore()
        return super().get_token(now=now)

    @property
    def expires_at(self) -> datetime | None:
        if self._token is None:
            try:
                self._restore()
            except TokenExpiredError:
                return None
        return super().expires_at
