"""Configurable, approval-ready swing signal evaluation and SQLite persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import Literal
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from database.models import Stock, TradeSignals
from database.repositories import TradeSignalRepository
from strategy.configuration import StrategyConfig, load_strategy_config
from strategy.indicators import IndicatorValues
from strategy.market_conditions import MarketConditions, classify_market_conditions
from strategy.technical_analysis import TechnicalAnalysisResult

SignalType = Literal["BUY", "HOLD", "NO TRADE"]
RecommendedAction = Literal["BUY", "HOLD", "NO TRADE", "EXIT"]
INDIA_TZ = ZoneInfo("Asia/Kolkata")


class IncompleteCandleError(ValueError):
    """Raised when callers attempt signal evaluation using a developing candle."""


@dataclass(frozen=True)
class OpenPositionContext:
    """Long paper-position state needed to evaluate completed-candle exits."""

    entry_price: float
    stop_loss: float | None
    target_price: float | None
    trailing_stop: float | None = None
    high_watermark: float | None = None


@dataclass(frozen=True)
class StrategySignal:
    signal_id: str
    stock_id: int
    symbol: str
    signal_type: SignalType
    signal_date: date
    candle_at: datetime
    entry_reference_price: float
    stop_loss: float | None
    target: float | None
    quantity_placeholder: None
    ema20: float
    ema50: float
    rsi14: float
    atr14: float
    volume_ratio: float
    risk_reward_ratio: float | None
    strategy_explanation: str
    signal_expiration: datetime
    recommended_action: RecommendedAction
    exit_reason: str | None = None
    next_trailing_stop: float | None = None
    next_high_watermark: float | None = None
    persisted: bool = False


def _stable_signal_id(stock_id: int, candle_at: datetime, version: str) -> str:
    identity = f"tradepilot:swing:{stock_id}:{candle_at.astimezone(timezone.utc).isoformat()}:{version}"
    return str(uuid5(NAMESPACE_URL, identity))


def _safe_level(value: float) -> float | None:
    return round(value, 4) if math.isfinite(value) and value > 0 else None


class SwingStrategyEngine:
    """Evaluate the configured long-only swing setup without placing orders."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or load_strategy_config()
        self.config.validate()

    def evaluate(self, stock: Stock, analysis: TechnicalAnalysisResult, *,
                 position: OpenPositionContext | None = None, manual_exit: bool = False) -> StrategySignal:
        """Build a single signal from a fully completed technical-analysis snapshot."""
        if not analysis.end_of_day:
            raise IncompleteCandleError("Swing strategy requires a completed end-of-day candle")
        if stock.id is None:
            raise ValueError("Stock must be persisted before generating a strategy signal")
        if manual_exit and position is None:
            raise ValueError("Manual exit can only be evaluated for an open position")
        if position is not None:
            optional_levels = (position.stop_loss, position.target_price,
                               position.trailing_stop, position.high_watermark)
            if (not math.isfinite(position.entry_price) or position.entry_price <= 0 or
                    any(value is not None and (not math.isfinite(value) or value <= 0)
                        for value in optional_levels)):
                raise ValueError("Open position contains invalid entry, stop, target, or trailing levels")
        values = analysis.latest
        numeric = (values.close, values.ema20, values.ema50, values.rsi14, values.atr14, values.relative_volume)
        if not all(math.isfinite(value) for value in numeric) or values.close <= 0 or values.atr14 <= 0:
            raise ValueError("Technical analysis contains invalid or incomplete strategy inputs")
        timestamp = values.timestamp
        if timestamp.tzinfo is None:
            raise ValueError("Completed candle timestamp must be timezone-aware")
        candle_at = timestamp.to_pydatetime().astimezone(timezone.utc)
        signal_date = timestamp.tz_convert(INDIA_TZ).date() if timestamp.tzinfo else timestamp.date()
        conditions = classify_market_conditions(values)
        row = analysis.frame.iloc[-1]
        market_reasons = self._market_filter_failures(values, conditions)
        entry_reasons = self._entry_failures(values, conditions, market_reasons)
        signal_type: SignalType
        recommended_action: RecommendedAction
        exit_reason = None
        next_trailing_stop = None
        next_high_watermark = None
        if position is not None:
            signal_type = "HOLD"
            exit_reason = self._exit_reason(position, row, values, manual_exit=manual_exit)
            recommended_action = "EXIT" if exit_reason else "HOLD"
            stop = position.stop_loss
            target = position.target_price
            if self.config.exits.trailing_stop_enabled:
                prior_high = position.high_watermark if position.high_watermark is not None else position.entry_price
                next_high_watermark = max(prior_high, float(row["high"]))
                candidate_trail = next_high_watermark - values.atr14 * self.config.exits.trailing_atr_multiple
                next_trailing_stop = _safe_level(max(position.trailing_stop or 0.0, candidate_trail))
            explanation = (f"Open position in {stock.symbol}. " +
                           (f"Exit condition: {exit_reason}. " if exit_reason else "No configured exit condition triggered. ") +
                           (f"Entry filters today: {'; '.join(entry_reasons)}." if entry_reasons else "Entry setup remains valid."))
        else:
            signal_type = "BUY" if not entry_reasons else "NO TRADE"
            recommended_action = signal_type
            stop = _safe_level(values.close - values.atr14 * self.config.exits.stop_atr_multiple)
            target = _safe_level(values.close + values.atr14 * self.config.exits.target_atr_multiple)
            if stop is None:
                entry_reasons.append("ATR-based stop-loss would not be a positive price")
                signal_type = "NO TRADE"
                recommended_action = "NO TRADE"
            explanation = (f"Entry conditions passed: EMA20 {values.ema20:.4f} > EMA50 {values.ema50:.4f}; "
                           f"close {values.close:.4f} above EMA20; RSI14 {values.rsi14:.2f} within "
                           f"[{self.config.entry.rsi_min:g}, {self.config.entry.rsi_max:g}]; "
                           f"relative volume {values.relative_volume:.2f} > "
                           f"{self.config.entry.relative_volume_min_exclusive:g}; "
                           f"market trend/volatility/momentum filters passed.") if signal_type == "BUY" else (
                "No trade: " + "; ".join(entry_reasons))
        risk_reward = None
        risk_reference = position.entry_price if position is not None else values.close
        if stop is not None and target is not None and risk_reference > stop and target > risk_reference:
            risk_reward = round((target - risk_reference) / (risk_reference - stop), 4)
        signal_id = _stable_signal_id(stock.id, candle_at, self.config.version)
        return StrategySignal(
            signal_id=signal_id, stock_id=stock.id, symbol=stock.symbol, signal_type=signal_type,
            signal_date=signal_date, candle_at=candle_at,
            entry_reference_price=position.entry_price if position is not None else values.close,
            stop_loss=stop, target=target, quantity_placeholder=None, ema20=values.ema20, ema50=values.ema50,
            rsi14=values.rsi14, atr14=values.atr14, volume_ratio=values.relative_volume,
            risk_reward_ratio=risk_reward, strategy_explanation=explanation,
            signal_expiration=candle_at + timedelta(days=self.config.signal_expiry_calendar_days),
            recommended_action=recommended_action, exit_reason=exit_reason,
            next_trailing_stop=next_trailing_stop, next_high_watermark=next_high_watermark,
        )

    def generate_and_store(self, session: Session, stock: Stock, analysis: TechnicalAnalysisResult, *,
                           position: OpenPositionContext | None = None, manual_exit: bool = False
                           ) -> StrategySignal:
        """Persist or return the existing signal for this stock and completed candle."""
        candidate = self.evaluate(stock, analysis, position=position, manual_exit=manual_exit)
        repository = TradeSignalRepository(session)
        existing = repository.get_for_candle(candidate.stock_id, candidate.candle_at)
        if existing is not None:
            return self._from_record(existing)
        record = TradeSignals(
            stock_id=candidate.stock_id, signal_key=candidate.signal_id, candle_at=candidate.candle_at,
            signal_date=candidate.signal_date, expires_at=candidate.signal_expiration,
            signal_type=candidate.signal_type, status="pending" if candidate.signal_type == "BUY" else "informational",
            rationale=candidate.strategy_explanation, reference_price=Decimal(str(candidate.entry_reference_price)),
            stop_loss=Decimal(str(candidate.stop_loss)) if candidate.stop_loss is not None else None,
            target_price=Decimal(str(candidate.target)) if candidate.target is not None else None,
            quantity_placeholder=candidate.quantity_placeholder,
            ema20=Decimal(str(candidate.ema20)), ema50=Decimal(str(candidate.ema50)),
            rsi14=Decimal(str(candidate.rsi14)), atr14=Decimal(str(candidate.atr14)),
            relative_volume=Decimal(str(candidate.volume_ratio)),
            risk_reward=Decimal(str(candidate.risk_reward_ratio)) if candidate.risk_reward_ratio is not None else None,
            next_trailing_stop=Decimal(str(candidate.next_trailing_stop)) if candidate.next_trailing_stop is not None else None,
            next_high_watermark=Decimal(str(candidate.next_high_watermark)) if candidate.next_high_watermark is not None else None,
            recommended_action=candidate.recommended_action, exit_reason=candidate.exit_reason,
            strategy_version=self.config.version,
        )
        stored, _ = repository.save_once(record)
        return self._from_record(stored)

    def _entry_failures(self, values: IndicatorValues, conditions: MarketConditions,
                        market_reasons: list[str] | None = None) -> list[str]:
        config = self.config
        failures = list(market_reasons or [])
        if values.ema20 <= values.ema50:
            failures.append("EMA20 is not above EMA50")
        if values.close <= values.ema20:
            failures.append("close is not above EMA20")
        if not config.entry.rsi_min <= values.rsi14 <= config.entry.rsi_max:
            failures.append(f"RSI14 is outside configured range {config.entry.rsi_min:g}–{config.entry.rsi_max:g}")
        if values.relative_volume <= config.entry.relative_volume_min_exclusive:
            failures.append(f"relative volume does not exceed {config.entry.relative_volume_min_exclusive:g}")
        return failures

    def _market_filter_failures(self, values: IndicatorValues, conditions: MarketConditions) -> list[str]:
        filters = self.config.market_filters
        reasons = []
        if conditions.trend not in filters.allowed_trends:
            reasons.append(f"market trend {conditions.trend!r} is filtered out")
        if conditions.volatility not in filters.allowed_volatility:
            reasons.append(f"market volatility {conditions.volatility!r} is filtered out")
        if conditions.momentum not in filters.allowed_momentum:
            reasons.append(f"market momentum {conditions.momentum!r} is filtered out")
        if conditions.volume_activity not in filters.allowed_volume_activity:
            reasons.append(f"volume activity {conditions.volume_activity!r} is filtered out")
        atr_percent = values.atr14 / values.close * 100
        if not filters.min_atr_percent <= atr_percent <= filters.max_atr_percent:
            reasons.append(f"ATR percent {atr_percent:.2f} is outside configured market bounds")
        if filters.require_close_above_support and values.close <= values.support20:
            reasons.append("close is not above prior support")
        return reasons

    def _exit_reason(self, position: OpenPositionContext, candle, values: IndicatorValues,
                     *, manual_exit: bool) -> str | None:
        config = self.config.exits
        low, high, close = float(candle["low"]), float(candle["high"]), values.close
        # If stop and target are touched in the same daily bar their order is unknown;
        # choose the protective stop first (conservative daily-bar assumption).
        if position.stop_loss is not None and low <= position.stop_loss:
            return "STOP_LOSS"
        if config.trailing_stop_enabled and position.trailing_stop is not None and low <= position.trailing_stop:
            return "TRAILING_STOP"
        if position.target_price is not None and high >= position.target_price:
            return "TARGET_REACHED"
        if (config.invalidate_when_ema20_below_ema50 and values.ema20 < values.ema50) or (
                config.invalidate_when_close_below_ema20 and close < values.ema20) or (
                values.rsi14 < config.invalidate_when_rsi_below):
            return "STRATEGY_INVALIDATION"
        if manual_exit:
            return "MANUAL_EXIT"
        return None

    @staticmethod
    def _from_record(record: TradeSignals) -> StrategySignal:
        return StrategySignal(
            signal_id=record.signal_key or f"trade-signal-{record.id}", stock_id=record.stock_id,
            symbol=record.stock.symbol, signal_type=record.signal_type,
            signal_date=record.signal_date or record.candle_at.astimezone(INDIA_TZ).date(),
            candle_at=record.candle_at, entry_reference_price=float(record.reference_price),
            stop_loss=float(record.stop_loss) if record.stop_loss is not None else None,
            target=float(record.target_price) if record.target_price is not None else None,
            quantity_placeholder=record.quantity_placeholder,
            ema20=float(record.ema20), ema50=float(record.ema50), rsi14=float(record.rsi14),
            atr14=float(record.atr14), volume_ratio=float(record.relative_volume),
            risk_reward_ratio=float(record.risk_reward) if record.risk_reward is not None else None,
            strategy_explanation=record.rationale or "", signal_expiration=record.expires_at,
            recommended_action=record.recommended_action or "HOLD", exit_reason=record.exit_reason,
            next_trailing_stop=float(record.next_trailing_stop) if record.next_trailing_stop is not None else None,
            next_high_watermark=float(record.next_high_watermark) if record.next_high_watermark is not None else None,
            persisted=True,
        )
