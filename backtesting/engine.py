"""Chronological daily-candle event-driven backtester for the configured swing rules."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd

from risk.engine import FeeSchedule, RiskConfig, RiskEngine, RiskRequest, estimate_delivery_costs
from strategy.configuration import StrategyConfig, load_strategy_config
from strategy.indicators import add_indicators, latest_indicator_values, validate_ohlcv
from strategy.swing_strategy import OpenPositionContext, SwingStrategyEngine
from strategy.technical_analysis import MINIMUM_HISTORY, TechnicalAnalysisResult
from database.models import Stock


class InsufficientBacktestData(ValueError):
    """The input does not contain enough usable completed daily candles."""


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: Decimal = Decimal("3000")
    slippage_bps: Decimal = Decimal("10")
    meaningful_minimum_bars: int = 252
    meaningful_minimum_trades: int = 30
    risk_config: RiskConfig | None = None

    def __post_init__(self) -> None:
        capital = Decimal(str(self.initial_capital))
        slip = Decimal(str(self.slippage_bps))
        if not capital.is_finite() or capital <= 0:
            raise ValueError("initial_capital must be finite and positive")
        if not slip.is_finite() or slip < 0 or slip > 1000:
            raise ValueError("slippage_bps must be between 0 and 1000")
        if self.meaningful_minimum_bars < MINIMUM_HISTORY or self.meaningful_minimum_trades < 1:
            raise ValueError("Meaningfulness thresholds are invalid")
        object.__setattr__(self, "initial_capital", capital)
        object.__setattr__(self, "slippage_bps", slip)


@dataclass(frozen=True)
class BacktestMetrics:
    initial_capital: float
    final_capital: float
    net_profit_loss: float
    total_return_pct: float
    annualized_return_pct: float | None
    win_rate_pct: float
    average_winning_trade: float
    average_losing_trade: float
    profit_factor: float | None
    maximum_drawdown_pct: float
    number_of_trades: int
    average_holding_period_sessions: float
    largest_loss: float | None
    largest_gain: float | None
    total_transaction_costs: float
    bars_tested: int


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    source: str
    metrics: BacktestMetrics
    trades: pd.DataFrame
    equity_curve: pd.DataFrame
    monthly_returns: pd.DataFrame
    regime_metrics: pd.DataFrame
    warnings: tuple[str, ...]
    completed_through: date
    strategy_version: str
    slippage_bps: float


@dataclass
class _Position:
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_reference_price: float
    entry_price: float
    quantity: int
    stop: float
    target: float
    buy_fee: float
    entry_slippage_cost: float
    high_watermark: float
    trailing_stop: float | None
    entry_regime: str
    holding_sessions: int = 1
    pending_exit: str | None = None


class HistoricalBacktester:
    """Run one fixed strategy configuration; no parameter optimization is performed."""

    def __init__(self, strategy_config: StrategyConfig | None = None,
                 backtest_config: BacktestConfig | None = None,
                 fee_schedule: FeeSchedule | None = None) -> None:
        self.strategy_config = strategy_config or load_strategy_config()
        self.strategy_config.validate()
        self.config = backtest_config or BacktestConfig()
        risk = self.config.risk_config or RiskConfig.from_settings()
        if fee_schedule is not None:
            risk = replace(risk, fees=fee_schedule)
        self.fees = risk.fees
        self.risk_config = risk
        self.signal_engine = SwingStrategyEngine(self.strategy_config)
        self.risk_engine = RiskEngine(risk)

    def run(self, ohlcv: pd.DataFrame, *, symbol: str,
            source: str = "user_supplied_historical_data",
            start_date: date | None = None, end_date: date | None = None) -> BacktestResult:
        if not symbol.strip():
            raise ValueError("symbol must not be blank")
        normalized, dropped = validate_ohlcv(ohlcv)
        if end_date is not None:
            normalized = normalized.loc[normalized.index.date <= end_date]
        if len(normalized) < MINIMUM_HISTORY:
            raise InsufficientBacktestData(
                f"Need at least {MINIMUM_HISTORY} complete daily candles; got {len(normalized)}")
        indicators = add_indicators(normalized)
        available = indicators.dropna(subset=["ema20", "ema50", "rsi14", "atr14", "volume_sma20",
                                               "relative_volume", "daily_change_pct", "support20", "resistance20"])
        if len(available) < 2:
            raise InsufficientBacktestData("History is too short to warm all strategy indicators and execute a next-session entry")
        effective_start = pd.Timestamp(start_date, tz="UTC") if start_date else available.index[0]
        if start_date and start_date not in [timestamp.date() for timestamp in available.index]:
            effective_start = next((timestamp for timestamp in available.index if timestamp.date() >= start_date), None)
            if effective_start is None:
                raise InsufficientBacktestData("No completed candles exist on or after start_date")
        if end_date and end_date < available.index[-1].date():
            available = available.loc[available.index.date <= end_date]
        if len(available) < 2:
            raise InsufficientBacktestData("The selected test period has fewer than two indicator-ready bars")

        stock = Stock(id=1, symbol=symbol.strip().upper(), name=symbol.strip().upper())
        cash = float(self.config.initial_capital)
        position: _Position | None = None
        pending_signal = None
        trades: list[dict] = []
        curve: list[dict] = []
        regimes = self._regimes(available)
        timeline = list(available.index)
        first_test_index = next(i for i, stamp in enumerate(timeline) if stamp >= effective_start)
        if first_test_index >= len(timeline) - 1:
            raise InsufficientBacktestData("The selected test period needs at least two bars after its start date")

        for i, stamp in enumerate(timeline):
            row = available.loc[stamp]
            # Orders from a completed prior candle fill at this session's open.
            if position is not None and position.pending_exit:
                if position.entry_date != stamp:
                    position.holding_sessions += 1
                fill = self._slipped(float(row["open"]), "sell")
                cash = self._close_position(position, stamp, fill, position.pending_exit, cash, trades,
                                            float(row["open"]))
                position = None
            if position is None and pending_signal is not None and i > 0:
                signal = pending_signal
                entry = self._slipped(float(row["open"]), "buy")
                stop, target = signal.stop_loss, signal.target
                # If an opening gap invalidates the signal's original reward/risk levels, skip it.
                stop_fill = self._slipped(float(stop), "sell")
                target_fill = self._slipped(float(target), "sell")
                risk = RiskRequest(entry_price=entry, stop_loss=stop_fill, target_price=target_fill,
                                   available_capital=max(cash, 0.01), available_cash=max(cash, 0),
                                   current_exposure=0, open_positions=0, daily_pnl=0,
                                   market_data_at=stamp.to_pydatetime(), as_of=stamp.to_pydatetime())
                assessment = self.risk_engine.assess(risk)
                if assessment.accepted:
                    qty = assessment.quantity
                    buy_fee, _ = estimate_delivery_costs(entry, entry, qty, self.fees)
                    required = entry * qty + float(buy_fee)
                    if required <= cash:
                        cash -= required
                        position = _Position(signal.candle_at, stamp, float(row["open"]), entry,
                                             qty, float(stop), float(target), float(buy_fee),
                                             max(entry - float(row["open"]), 0.0) * qty, entry, None,
                                             regimes.get(signal.candle_at, "Unknown"))
                pending_signal = None

            if position is not None:
                if position.entry_date != stamp:
                    position.holding_sessions += 1
                hit, exit_raw = self._intraday_exit(position, row)
                if hit:
                    exit_fill = self._slipped(exit_raw, "sell")
                    cash = self._close_position(position, stamp, exit_fill, hit, cash, trades, exit_raw)
                    position = None
                else:
                    prefix = available.iloc[:i + 1]
                    values = latest_indicator_values(prefix)
                    analysis = TechnicalAnalysisResult(prefix, values, dropped, 0, True, source)
                    context = OpenPositionContext(position.entry_price, position.stop, position.target,
                                                  position.trailing_stop, position.high_watermark)
                    exit_signal = self.signal_engine.evaluate(stock, analysis, position=context)
                    if exit_signal.recommended_action == "EXIT":
                        position.pending_exit = exit_signal.exit_reason or "STRATEGY_INVALIDATION"
                    if self.strategy_config.exits.trailing_stop_enabled:
                        position.high_watermark = max(position.high_watermark, float(row["high"]))
                        trailing = position.high_watermark - values.atr14 * self.strategy_config.exits.trailing_atr_multiple
                        position.trailing_stop = max(position.trailing_stop or 0.0, trailing)

            # Completed-bar evaluation only; a signal becomes eligible at the next open.
            if position is None and pending_signal is None and i >= first_test_index and i < len(timeline) - 1:
                prefix = available.iloc[:i + 1]
                try:
                    values = latest_indicator_values(prefix)
                    analysis = TechnicalAnalysisResult(prefix, values, dropped, 0, True, source)
                    signal = self.signal_engine.evaluate(stock, analysis)
                    if signal.recommended_action == "BUY":
                        pending_signal = signal
                except ValueError:
                    pass
            mark = float(row["close"])
            equity = cash + (position.quantity * mark if position is not None else 0.0)
            if stamp >= effective_start:
                curve.append({"date": stamp, "cash": cash, "market_value": position.quantity * mark if position else 0.0,
                              "equity": equity, "drawdown_pct": 0.0})

        # Liquidate any remaining position at last completed close, including sell-side costs.
        if position is not None:
            last_stamp = timeline[-1]
            exit_reference = float(available.iloc[-1]["close"])
            exit_fill = self._slipped(exit_reference, "sell")
            cash = self._close_position(position, last_stamp, exit_fill, "END_OF_DATA", cash, trades,
                                        exit_reference)
            position = None
            curve[-1].update({"cash": cash, "market_value": 0.0, "equity": cash})

        curve_frame = pd.DataFrame(curve).set_index("date")
        curve_frame["peak"] = curve_frame["equity"].cummax()
        curve_frame["drawdown_pct"] = (curve_frame["equity"] / curve_frame["peak"] - 1.0) * 100.0
        trade_frame = pd.DataFrame(trades, columns=["symbol", "signal_date", "entry_date", "exit_date",
            "entry_reference_price", "entry_price", "exit_reference_price", "exit_price", "quantity", "stop_loss",
            "target", "gross_pnl", "buy_cost", "sell_cost", "transaction_costs", "entry_slippage_cost",
            "exit_slippage_cost", "total_slippage_cost", "total_costs", "net_pnl",
            "holding_period_sessions", "exit_reason", "entry_regime"])
        if not trade_frame.empty:
            trade_frame["symbol"] = symbol.strip().upper()
        metrics = self._metrics(float(self.config.initial_capital), float(curve_frame["equity"].iloc[-1]),
                                curve_frame, trade_frame, len(timeline) - first_test_index)
        monthly = self._monthly_returns(curve_frame, float(self.config.initial_capital))
        regime_metrics = self._regime_metrics(trade_frame)
        warnings = []
        if len(timeline) - first_test_index < self.config.meaningful_minimum_bars:
            warnings.append(f"Only {len(timeline) - first_test_index} test bars; {self.config.meaningful_minimum_bars} are recommended for more meaningful historical context.")
        if len(trade_frame) < self.config.meaningful_minimum_trades:
            warnings.append(f"Only {len(trade_frame)} completed trades; {self.config.meaningful_minimum_trades} are recommended before interpreting performance metrics.")
        if dropped:
            warnings.append(f"Dropped {dropped} incomplete OHLCV row(s); verify the source dataset before relying on results.")
        return BacktestResult(symbol.strip().upper(), source, metrics, trade_frame, curve_frame,
                              monthly, regime_metrics, tuple(warnings), available.index[-1].date(),
                              self.strategy_config.version, float(self.config.slippage_bps))

    def _intraday_exit(self, position: _Position, row: pd.Series) -> tuple[str | None, float]:
        low, high, open_price = float(row["low"]), float(row["high"]), float(row["open"])
        stop_levels = [position.stop]
        if position.trailing_stop is not None:
            stop_levels.append(position.trailing_stop)
        stop = max(stop_levels)
        if open_price <= stop:
            return ("TRAILING_STOP" if position.trailing_stop is not None and stop == position.trailing_stop else "STOP_LOSS"), open_price
        if low <= stop and high >= position.target:
            return "STOP_LOSS", stop  # Daily OHLC cannot establish which level came first.
        if low <= stop:
            return ("TRAILING_STOP" if position.trailing_stop is not None and stop == position.trailing_stop else "STOP_LOSS"), stop
        if open_price >= position.target:
            return "TARGET_REACHED", open_price
        if high >= position.target:
            return "TARGET_REACHED", position.target
        return None, 0.0

    def _close_position(self, position: _Position, stamp: pd.Timestamp, exit_price: float,
                        reason: str, cash: float, trades: list[dict],
                        exit_reference_price: float) -> float:
        _, round_trip = estimate_delivery_costs(position.entry_price, exit_price, position.quantity, self.fees)
        sell_cost = max(float(round_trip) - position.buy_fee, 0.0)
        proceeds = exit_price * position.quantity - sell_cost
        cash += proceeds
        gross = (exit_reference_price - position.entry_reference_price) * position.quantity
        exit_slippage = max(exit_reference_price - exit_price, 0.0) * position.quantity
        slippage = position.entry_slippage_cost + exit_slippage
        net = gross - slippage - position.buy_fee - sell_cost
        trades.append({"symbol": "", "signal_date": position.signal_date.date(), "entry_date": position.entry_date.date(),
                       "exit_date": stamp.date(), "entry_reference_price": position.entry_reference_price,
                       "entry_price": position.entry_price, "exit_reference_price": exit_reference_price,
                       "exit_price": exit_price,
                       "quantity": position.quantity, "stop_loss": position.stop, "target": position.target,
                       "gross_pnl": gross, "buy_cost": position.buy_fee, "sell_cost": sell_cost,
                       "transaction_costs": position.buy_fee + sell_cost,
                       "entry_slippage_cost": position.entry_slippage_cost,
                       "exit_slippage_cost": exit_slippage, "total_slippage_cost": slippage,
                       "total_costs": position.buy_fee + sell_cost + slippage, "net_pnl": net,
                       "holding_period_sessions": position.holding_sessions,
                       "exit_reason": reason, "entry_regime": position.entry_regime})
        return cash

    def _slipped(self, price: float, side: str) -> float:
        factor = 1 + float(self.config.slippage_bps) / 10000
        return price * factor if side == "buy" else price / factor

    @staticmethod
    def _metrics(initial: float, final: float, curve: pd.DataFrame,
                 trades: pd.DataFrame, bars: int) -> BacktestMetrics:
        pnl = trades["net_pnl"] if not trades.empty else pd.Series(dtype=float)
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        days = (curve.index[-1].date() - curve.index[0].date()).days if len(curve) > 1 else 0
        annualized = ((final / initial) ** (365.25 / days) - 1) * 100 if days > 0 and final > 0 else None
        gross_loss = abs(float(losses.sum())) if len(losses) else 0.0
        pf = float(wins.sum()) / gross_loss if gross_loss else None
        return BacktestMetrics(initial, final, final - initial, (final / initial - 1) * 100,
                               annualized, len(wins) / len(pnl) * 100 if len(pnl) else 0.0,
                               float(wins.mean()) if len(wins) else 0.0,
                               float(losses.mean()) if len(losses) else 0.0, pf,
                               float(curve["drawdown_pct"].min()), len(pnl),
                               float(trades["holding_period_sessions"].mean()) if len(pnl) else 0.0,
                               float(losses.min()) if len(losses) else None,
                               float(wins.max()) if len(wins) else None,
                               float(trades["total_costs"].sum()) if len(pnl) else 0.0, bars)

    @staticmethod
    def _monthly_returns(curve: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
        if curve.empty:
            return pd.DataFrame(columns=["month", "return_pct"])
        monthly = curve["equity"].resample("ME").last()
        result = (monthly.pct_change() * 100)
        if len(result):
            result.iloc[0] = (monthly.iloc[0] / initial_capital - 1) * 100
        result = result.rename("return_pct").reset_index()
        return result.rename(columns={"date": "month"})

    @staticmethod
    def _regimes(frame: pd.DataFrame) -> dict[pd.Timestamp, str]:
        trend = frame["ema50"].pct_change(20)
        regimes = np.select([(frame["close"] > frame["ema50"]) & (trend > 0.01),
                             (frame["close"] < frame["ema50"]) & (trend < -0.01)],
                            ["Bull", "Bear"], default="Sideways")
        return dict(zip(frame.index, regimes, strict=True))

    @staticmethod
    def _regime_metrics(trades: pd.DataFrame) -> pd.DataFrame:
        if trades.empty:
            return pd.DataFrame(columns=["regime", "trades", "net_pnl", "win_rate_pct"])
        return (trades.groupby("entry_regime", dropna=False).agg(
            trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
            win_rate_pct=("net_pnl", lambda values: (values > 0).mean() * 100))
                .reset_index().rename(columns={"entry_regime": "regime"}))
