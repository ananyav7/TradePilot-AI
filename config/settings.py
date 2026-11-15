"""Central, environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from decimal import Decimal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Settings loaded from environment variables and an optional .env file."""

    app_name: str = "TradePilot AI"
    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = Field(default="sqlite:///./data/tradepilot.db")
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    paper_trading_enabled: bool = True
    live_trading_enabled: bool = False
    upstox_client_id: SecretStr = Field(default=SecretStr(""), repr=False)
    upstox_client_secret: SecretStr = Field(default=SecretStr(""), repr=False)
    upstox_redirect_uri: str = "http://localhost:8000/auth/callback"
    upstox_api_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    upstox_max_read_retries: int = Field(default=3, ge=1, le=5)
    upstox_instrument_cache_path: Path = PROJECT_ROOT / "data" / "cache" / "upstox_nse.json.gz"
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), repr=False)
    telegram_chat_id: str = ""
    ai_provider: str = "disabled"
    ai_model: str = ""
    ai_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    ai_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    research_market_news_feeds: list[str] = Field(default_factory=lambda: [
        "https://www.business-standard.com/rss/markets-106.rss"])
    research_announcement_feeds: dict[str, list[str]] = Field(default_factory=dict)
    risk_starting_capital: Decimal = Decimal("3000")
    risk_per_trade: Decimal = Decimal("0.005")
    risk_max_open_positions: int = Field(default=1, ge=1)
    risk_max_portfolio_exposure: Decimal = Decimal("1.0")
    risk_min_reward_to_risk: Decimal = Decimal("1.5")
    risk_daily_loss_limit: Decimal = Decimal("0.015")
    risk_max_market_data_age_minutes: int = Field(default=1440, ge=1)
    risk_leverage_enabled: bool = False
    risk_derivatives_enabled: bool = False
    risk_brokerage_rate: Decimal = Decimal("0.0003")
    risk_brokerage_cap_per_leg: Decimal = Decimal("20")
    risk_stt_sell_rate: Decimal = Decimal("0.001")
    risk_exchange_rate: Decimal = Decimal("0.0000297")
    risk_sebi_rate: Decimal = Decimal("0.000001")
    risk_gst_rate: Decimal = Decimal("0.18")
    risk_stamp_buy_rate: Decimal = Decimal("0.00015")
    risk_depository_sell_fee: Decimal = Decimal("0")
    paper_slippage_bps: Decimal = Decimal("10")
    paper_max_quote_age_seconds: int = Field(default=30, ge=1)
    daily_scheduler_enabled: bool = True
    scheduler_monitor_interval_minutes: int = Field(default=5, ge=1, le=60)

    @field_validator("live_trading_enabled")
    @classmethod
    def live_trading_must_remain_disabled(cls, value: bool) -> bool:
        """Accept false from dotenv while making every enable attempt invalid."""
        if value:
            raise ValueError("Live trading is permanently disabled in this version")
        return False

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
