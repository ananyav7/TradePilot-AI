"""Deterministic, long-only delivery risk calculations. This module never sends orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation

MONEY = Decimal("0.01")
ZERO = Decimal("0")


def _decimal(value: Decimal | int | float | str, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite number")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class FeeSchedule:
    """Configurable estimate rates; actual broker contract notes are authoritative."""

    brokerage_rate: Decimal = Decimal("0.0003")
    brokerage_cap_per_leg: Decimal = Decimal("20")
    stt_sell_rate: Decimal = Decimal("0.001")
    exchange_rate: Decimal = Decimal("0.0000297")
    sebi_rate: Decimal = Decimal("0.000001")
    gst_rate: Decimal = Decimal("0.18")
    stamp_buy_rate: Decimal = Decimal("0.00015")
    depository_sell_fee: Decimal = ZERO

    def __post_init__(self) -> None:
        for name in ("brokerage_rate", "brokerage_cap_per_leg", "stt_sell_rate", "exchange_rate",
                     "sebi_rate", "gst_rate", "stamp_buy_rate", "depository_sell_fee"):
            value = _decimal(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)


def estimate_delivery_costs(buy_price: Decimal | float | str,
                            sell_price: Decimal | float | str,
                            quantity: int,
                            fees: FeeSchedule | None = None) -> tuple[Decimal, Decimal]:
    """Return (buy-leg cost, round-trip cost) using the shared configurable estimates."""
    buy = _decimal(buy_price, "buy_price")
    sell = _decimal(sell_price, "sell_price")
    if buy <= 0 or sell <= 0 or isinstance(quantity, bool) or quantity < 0:
        raise ValueError("Prices must be positive and quantity must be a non-negative integer")
    if not isinstance(quantity, int):
        raise ValueError("Quantity must be an integer")
    f = fees or FeeSchedule()
    buy_turnover, sell_turnover = buy * quantity, sell * quantity
    buy_brokerage = min(buy_turnover * f.brokerage_rate, f.brokerage_cap_per_leg)
    sell_brokerage = min(sell_turnover * f.brokerage_rate, f.brokerage_cap_per_leg)
    buy_exchange, sell_exchange = buy_turnover * f.exchange_rate, sell_turnover * f.exchange_rate
    buy_sebi, sell_sebi = buy_turnover * f.sebi_rate, sell_turnover * f.sebi_rate
    buy_gst = (buy_brokerage + buy_exchange + buy_sebi) * f.gst_rate
    sell_gst = (sell_brokerage + sell_exchange + sell_sebi) * f.gst_rate
    buy_fee = _money(buy_brokerage + buy_exchange + buy_sebi + buy_gst + buy_turnover * f.stamp_buy_rate)
    sell_fee = _money(sell_brokerage + sell_exchange + sell_sebi + sell_gst +
                      sell_turnover * f.stt_sell_rate + f.depository_sell_fee)
    return buy_fee, buy_fee + sell_fee


def estimate_sell_cost(sell_price: Decimal | float | str, quantity: int,
                       fees: FeeSchedule | None = None) -> Decimal:
    """Estimate sell-side brokerage, exchange/SEBI fees, GST, STT and depository fee."""
    price = _decimal(sell_price, "sell_price")
    if price <= 0 or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ValueError("Sell price must be positive and quantity a non-negative integer")
    f = fees or FeeSchedule()
    turnover = price * quantity
    brokerage = min(turnover * f.brokerage_rate, f.brokerage_cap_per_leg)
    exchange, sebi = turnover * f.exchange_rate, turnover * f.sebi_rate
    gst = (brokerage + exchange + sebi) * f.gst_rate
    return _money(brokerage + exchange + sebi + gst + turnover * f.stt_sell_rate + f.depository_sell_fee)


@dataclass(frozen=True)
class RiskConfig:
    starting_capital: Decimal = Decimal("3000")
    risk_per_trade: Decimal = Decimal("0.005")
    max_open_positions: int = 1
    max_portfolio_exposure: Decimal = Decimal("1.0")
    min_risk_reward: Decimal = Decimal("1.5")
    daily_loss_limit: Decimal = Decimal("0.015")
    max_market_data_age: timedelta = timedelta(hours=24)
    leverage_enabled: bool = False
    derivatives_enabled: bool = False
    fees: FeeSchedule = field(default_factory=FeeSchedule)

    @classmethod
    def from_settings(cls, settings=None) -> "RiskConfig":
        """Build validated limits from central application settings."""
        if settings is None:
            from config.settings import get_settings
            settings = get_settings()
        fees = FeeSchedule(
            brokerage_rate=settings.risk_brokerage_rate,
            brokerage_cap_per_leg=settings.risk_brokerage_cap_per_leg,
            stt_sell_rate=settings.risk_stt_sell_rate,
            exchange_rate=settings.risk_exchange_rate,
            sebi_rate=settings.risk_sebi_rate,
            gst_rate=settings.risk_gst_rate,
            stamp_buy_rate=settings.risk_stamp_buy_rate,
            depository_sell_fee=settings.risk_depository_sell_fee,
        )
        return cls(
            starting_capital=settings.risk_starting_capital,
            risk_per_trade=settings.risk_per_trade,
            max_open_positions=settings.risk_max_open_positions,
            max_portfolio_exposure=settings.risk_max_portfolio_exposure,
            min_risk_reward=settings.risk_min_reward_to_risk,
            daily_loss_limit=settings.risk_daily_loss_limit,
            max_market_data_age=timedelta(minutes=settings.risk_max_market_data_age_minutes),
            leverage_enabled=settings.risk_leverage_enabled,
            derivatives_enabled=settings.risk_derivatives_enabled,
            fees=fees,
        )

    def __post_init__(self) -> None:
        for key in ("starting_capital", "risk_per_trade", "max_portfolio_exposure", "min_risk_reward", "daily_loss_limit"):
            value = _decimal(getattr(self, key), key)
            if value <= 0:
                raise ValueError(f"{key} must be positive")
            object.__setattr__(self, key, value)
        if self.risk_per_trade > 1 or self.max_portfolio_exposure > 1 or self.daily_loss_limit > 1:
            raise ValueError("Risk, exposure, and daily-loss percentages must not exceed 100%")
        if self.max_open_positions < 1 or self.max_market_data_age <= timedelta(0):
            raise ValueError("Position limit and market-data age must be positive")
        if self.leverage_enabled or self.derivatives_enabled:
            raise ValueError("TradePilot risk engine supports unleveraged delivery equities only")


@dataclass(frozen=True)
class RiskRequest:
    entry_price: Decimal | float | str
    stop_loss: Decimal | float | str
    target_price: Decimal | float | str
    available_capital: Decimal | float | str
    available_cash: Decimal | float | str
    current_exposure: Decimal | float | str = ZERO
    open_positions: int = 0
    daily_pnl: Decimal | float | str = ZERO
    market_data_at: datetime | None = None
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class RiskAssessment:
    accepted: bool
    reasons: tuple[str, ...]
    quantity: int
    risk_amount: Decimal
    risk_per_share: Decimal
    stop_loss_distance: Decimal
    target_distance: Decimal
    risk_reward_ratio: Decimal | None
    capital_requirement: Decimal
    buy_transaction_cost: Decimal
    round_trip_transaction_cost: Decimal
    gross_expected_reward: Decimal
    net_expected_reward: Decimal
    portfolio_exposure_after: Decimal
    exposure_percent_after: Decimal
    transaction_costs_are_estimates: bool = True
    risk_amount_includes_estimated_costs: bool = True


class RiskEngine:
    """Calculate share quantity and reject proposals that violate configured limits."""

    def __init__(self, config: RiskConfig | None = None, kill_switch=None) -> None:
        self.config = config or RiskConfig.from_settings()
        self.kill_switch = kill_switch

    def assess(self, request: RiskRequest) -> RiskAssessment:
        entry = _decimal(request.entry_price, "entry_price")
        stop = _decimal(request.stop_loss, "stop_loss")
        target = _decimal(request.target_price, "target_price")
        capital = _decimal(request.available_capital, "available_capital")
        cash = _decimal(request.available_cash, "available_cash")
        exposure = _decimal(request.current_exposure, "current_exposure")
        daily_pnl = _decimal(request.daily_pnl, "daily_pnl")
        reasons: list[str] = []
        cfg = self.config
        if entry <= ZERO:
            reasons.append("entry_price_invalid")
        if stop <= ZERO or entry <= stop:
            reasons.append("stop_loss_invalid")
        if target <= entry:
            reasons.append("target_price_invalid")
        if capital <= ZERO or cash < ZERO or exposure < ZERO:
            reasons.append("capital_or_cash_invalid")
        if isinstance(request.open_positions, bool) or request.open_positions < 0:
            reasons.append("open_position_count_invalid")
        if request.open_positions >= cfg.max_open_positions:
            reasons.append("maximum_open_positions_reached")
        if self.kill_switch is not None and self.kill_switch.is_active:
            reasons.append("kill_switch_active")
        daily_loss_cap = capital * cfg.daily_loss_limit
        if daily_pnl <= -daily_loss_cap:
            reasons.append("daily_loss_limit_reached")
        if request.market_data_at is None:
            reasons.append("market_data_timestamp_missing")
        elif request.market_data_at.tzinfo is None or request.as_of.tzinfo is None:
            reasons.append("market_data_timestamp_invalid")
        else:
            age = request.as_of.astimezone(timezone.utc) - request.market_data_at.astimezone(timezone.utc)
            if age < timedelta(0) or age > cfg.max_market_data_age:
                reasons.append("market_data_stale")

        risk_per_share = max(entry - stop, ZERO)
        stop_distance = risk_per_share
        target_distance = max(target - entry, ZERO)
        rr = target_distance / risk_per_share if risk_per_share > ZERO else None
        if rr is not None and rr < cfg.min_risk_reward:
            reasons.append("risk_reward_below_minimum")
        if reasons:
            return self._result(False, reasons, 0, ZERO, risk_per_share, stop_distance,
                                target_distance, rr, exposure)

        risk_budget = capital * cfg.risk_per_trade
        risk_quantity = self._max_quantity_for_risk(entry, stop, risk_budget)
        cash_quantity = self._max_quantity_for_cash(entry, cash)
        exposure_limit = capital * cfg.max_portfolio_exposure
        exposure_quantity = int((max(exposure_limit - exposure, ZERO) / entry).to_integral_value(rounding=ROUND_FLOOR))
        quantity = min(risk_quantity, cash_quantity, exposure_quantity)
        if quantity <= 0:
            if risk_quantity > 0 and cash_quantity == 0:
                reasons.append("required_capital_exceeds_available_cash")
            if risk_quantity > 0 and exposure_quantity == 0:
                reasons.append("portfolio_exposure_limit_exceeded")
            reasons.append("quantity_is_zero")
            return self._result(False, reasons, 0, ZERO, risk_per_share, stop_distance,
                                target_distance, rr, exposure)

        buy_cost = self._fees(entry, entry, quantity)[0]
        capital_requirement = _money(entry * quantity) + buy_cost
        if capital_requirement > cash:
            reasons.append("required_capital_exceeds_available_cash")
        stop_round_trip_cost = self._fees(entry, stop, quantity)[1]
        risk_amount = _money(risk_per_share * quantity + stop_round_trip_cost)
        if risk_amount > risk_budget:
            reasons.append("maximum_permitted_risk_exceeded")
        after = exposure + entry * quantity
        if after > exposure_limit:
            reasons.append("portfolio_exposure_limit_exceeded")
        buy_cost, round_trip_cost = self._fees(entry, target, quantity)
        gross_reward = target_distance * quantity
        net_reward = gross_reward - round_trip_cost
        return RiskAssessment(not reasons, tuple(reasons), quantity, risk_amount,
                              risk_per_share, stop_distance, target_distance, rr,
                              capital_requirement, buy_cost, round_trip_cost,
                              _money(gross_reward), _money(net_reward), _money(after),
                              (after / capital * 100).quantize(Decimal("0.01")))

    def _max_quantity_for_cash(self, entry: Decimal, cash: Decimal) -> int:
        """Find largest affordable whole-share quantity including buy-leg fees."""
        quantity = int((cash / entry).to_integral_value(rounding=ROUND_FLOOR))
        while quantity > 0:
            fee = self._fees(entry, entry, quantity)[0]
            if entry * quantity + fee <= cash:
                return quantity
            quantity -= 1
        return 0

    def _max_quantity_for_risk(self, entry: Decimal, stop: Decimal, risk_budget: Decimal) -> int:
        per_share_risk = entry - stop
        quantity = int((risk_budget / per_share_risk).to_integral_value(rounding=ROUND_FLOOR))
        while quantity > 0:
            estimated_loss = per_share_risk * quantity + self._fees(entry, stop, quantity)[1]
            if _money(estimated_loss) <= risk_budget:
                return quantity
            quantity -= 1
        return 0

    def _fees(self, buy_price: Decimal, sell_price: Decimal, quantity: int) -> tuple[Decimal, Decimal]:
        return estimate_delivery_costs(buy_price, sell_price, quantity, self.config.fees)

    def _result(self, accepted: bool, reasons: list[str], quantity: int,
                risk_amount: Decimal, per_share: Decimal, stop_distance: Decimal,
                target_distance: Decimal, rr: Decimal | None, exposure: Decimal) -> RiskAssessment:
        return RiskAssessment(accepted, tuple(reasons), quantity, risk_amount,
                              per_share, stop_distance, target_distance, rr,
                              ZERO, ZERO, ZERO, ZERO, ZERO, exposure, ZERO)
