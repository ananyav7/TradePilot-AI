"""Validated TOML-backed settings for the experimental swing strategy."""

from dataclasses import dataclass
import math
from numbers import Real
from pathlib import Path
import tomllib

DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("strategy_config.toml")
VALID_TRENDS = {"Bullish", "Bearish", "Sideways / mixed"}
VALID_VOLATILITY = {"Low", "Normal", "High"}
VALID_MOMENTUM = {"Overbought", "Oversold", "Neutral"}
VALID_VOLUME_ACTIVITY = {"Quiet", "Typical", "Strong"}


@dataclass(frozen=True)
class EntryConfig:
    rsi_min: float = 50.0
    rsi_max: float = 65.0
    relative_volume_min_exclusive: float = 1.2


@dataclass(frozen=True)
class MarketFilterConfig:
    allowed_trends: tuple[str, ...] = ("Bullish",)
    allowed_volatility: tuple[str, ...] = ("Low", "Normal", "High")
    allowed_momentum: tuple[str, ...] = ("Neutral",)
    allowed_volume_activity: tuple[str, ...] = ("Typical", "Strong")
    min_atr_percent: float = 0.0
    max_atr_percent: float = 10.0
    require_close_above_support: bool = False


@dataclass(frozen=True)
class ExitConfig:
    stop_atr_multiple: float = 2.0
    target_atr_multiple: float = 3.0
    trailing_stop_enabled: bool = True
    trailing_atr_multiple: float = 2.0
    invalidate_when_ema20_below_ema50: bool = True
    invalidate_when_close_below_ema20: bool = True
    invalidate_when_rsi_below: float = 45.0


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "EMA-RSI-ATR-Volume Swing"
    version: str = "swing-v1"
    signal_expiry_calendar_days: int = 3
    entry: EntryConfig = EntryConfig()
    market_filters: MarketFilterConfig = MarketFilterConfig()
    exits: ExitConfig = ExitConfig()

    @classmethod
    def from_file(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "StrategyConfig":
        config_path = Path(path)
        try:
            with config_path.open("rb") as stream:
                document = tomllib.load(stream)
        except OSError as exc:
            raise ValueError(f"Unable to read swing strategy configuration: {config_path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid swing strategy TOML: {config_path}") from exc
        try:
            general = document["strategy"]
            entry = EntryConfig(**document["entry"])
            market = document["market_filters"]
            filters = MarketFilterConfig(
                allowed_trends=tuple(market["allowed_trends"]),
                allowed_volatility=tuple(market["allowed_volatility"]),
                allowed_momentum=tuple(market["allowed_momentum"]),
                allowed_volume_activity=tuple(market["allowed_volume_activity"]),
                min_atr_percent=market["min_atr_percent"], max_atr_percent=market["max_atr_percent"],
                require_close_above_support=market["require_close_above_support"],
            )
            exits = ExitConfig(**document["exits"])
            config = cls(general["name"], general["version"], general["signal_expiry_calendar_days"],
                         entry, filters, exits)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Strategy configuration is missing or has invalid fields") from exc
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.version, str) or not self.name.strip() or not self.version.strip():
            raise ValueError("Strategy name and version must not be blank")
        if (isinstance(self.signal_expiry_calendar_days, bool) or
                not isinstance(self.signal_expiry_calendar_days, int) or self.signal_expiry_calendar_days < 1):
            raise ValueError("signal_expiry_calendar_days must be a positive integer")
        entry_values = (self.entry.rsi_min, self.entry.rsi_max, self.entry.relative_volume_min_exclusive)
        if not all(isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) for value in entry_values):
            raise ValueError("Entry thresholds must be finite numbers")
        if not 0 <= self.entry.rsi_min < self.entry.rsi_max <= 100:
            raise ValueError("RSI bounds must satisfy 0 <= rsi_min < rsi_max <= 100")
        if self.entry.relative_volume_min_exclusive < 0:
            raise ValueError("Relative-volume threshold cannot be negative")
        market = self.market_filters
        if not all(isinstance(value, str) for value in market.allowed_trends) or not set(market.allowed_trends) <= VALID_TRENDS or not market.allowed_trends:
            raise ValueError("allowed_trends must contain recognized market trend labels")
        if not all(isinstance(value, str) for value in market.allowed_volatility) or not set(market.allowed_volatility) <= VALID_VOLATILITY or not market.allowed_volatility:
            raise ValueError("allowed_volatility must contain recognized volatility labels")
        if not all(isinstance(value, str) for value in market.allowed_momentum) or not set(market.allowed_momentum) <= VALID_MOMENTUM or not market.allowed_momentum:
            raise ValueError("allowed_momentum must contain recognized momentum labels")
        if not all(isinstance(value, str) for value in market.allowed_volume_activity) or not set(market.allowed_volume_activity) <= VALID_VOLUME_ACTIVITY or not market.allowed_volume_activity:
            raise ValueError("allowed_volume_activity must contain recognized volume labels")
        market_values = (market.min_atr_percent, market.max_atr_percent)
        if not all(isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) for value in market_values):
            raise ValueError("Market ATR-percent filters must be finite")
        if market.min_atr_percent < 0 or market.min_atr_percent > market.max_atr_percent:
            raise ValueError("Market ATR-percent bounds are invalid")
        if not isinstance(market.require_close_above_support, bool):
            raise ValueError("require_close_above_support must be boolean")
        exits = self.exits
        exit_numbers = (exits.stop_atr_multiple, exits.target_atr_multiple,
                        exits.trailing_atr_multiple, exits.invalidate_when_rsi_below)
        if not all(isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value) for value in exit_numbers):
            raise ValueError("Exit thresholds must be finite numbers")
        if min(exits.stop_atr_multiple, exits.target_atr_multiple, exits.trailing_atr_multiple) <= 0:
            raise ValueError("ATR multiples must be positive")
        if not 0 <= exits.invalidate_when_rsi_below <= 100:
            raise ValueError("invalidate_when_rsi_below must be between 0 and 100")
        if not isinstance(exits.trailing_stop_enabled, bool):
            raise ValueError("trailing_stop_enabled must be boolean")
        if (not isinstance(exits.invalidate_when_ema20_below_ema50, bool) or
                not isinstance(exits.invalidate_when_close_below_ema20, bool)):
            raise ValueError("Strategy invalidation options must be boolean")


def load_strategy_config(path: Path | str = DEFAULT_CONFIG_PATH) -> StrategyConfig:
    """Load and validate the user-editable strategy TOML file."""
    return StrategyConfig.from_file(path)
