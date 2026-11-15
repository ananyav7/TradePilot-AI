from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from risk.controls import KillSwitch
from risk.engine import FeeSchedule, RiskConfig, RiskEngine, RiskRequest


NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)


def request(**overrides):
    values = dict(entry_price="100", stop_loss="96", target_price="106",
                  available_capital="3000", available_cash="3000",
                  current_exposure="0", open_positions=0, daily_pnl="0",
                  market_data_at=NOW, as_of=NOW)
    values.update(overrides)
    return RiskRequest(**values)


def test_position_size_respects_risk_and_rounds_down_whole_shares():
    result = RiskEngine(RiskConfig()).assess(request())
    assert result.accepted
    assert result.quantity == 3  # ₹15 risk budget / ₹4 per share, rounded down.
    assert result.risk_amount > Decimal("12.00")  # Includes estimated round-trip costs at the stop.
    assert result.risk_amount <= Decimal("3000") * Decimal("0.005")
    assert result.stop_loss_distance == Decimal("4")
    assert result.target_distance == Decimal("6")
    assert result.risk_reward_ratio == Decimal("1.5")


def test_cash_limit_includes_buy_side_costs():
    result = RiskEngine().assess(request(available_cash="300"))
    assert result.accepted
    assert result.quantity == 2  # Three shares exceed ₹300 after estimated charges.
    assert result.capital_requirement <= Decimal("300")
    assert result.buy_transaction_cost > 0


def test_insufficient_cash_for_one_share_is_rejected():
    result = RiskEngine().assess(request(entry_price="1000", stop_loss="990", target_price="1015",
                                         available_cash="1000.01", available_capital="3000"))
    assert not result.accepted
    assert "required_capital_exceeds_available_cash" in result.reasons
    assert "quantity_is_zero" in result.reasons


@pytest.mark.parametrize(("changes", "reason"), [
    ({"entry_price": "0"}, "entry_price_invalid"),
    ({"stop_loss": "100"}, "stop_loss_invalid"),
    ({"stop_loss": "101"}, "stop_loss_invalid"),
    ({"target_price": "105"}, "risk_reward_below_minimum"),
    ({"open_positions": 1}, "maximum_open_positions_reached"),
    ({"daily_pnl": "-45"}, "daily_loss_limit_reached"),
    ({"market_data_at": NOW - timedelta(hours=25)}, "market_data_stale"),
    ({"market_data_at": NOW + timedelta(minutes=1)}, "market_data_stale"),
    ({"current_exposure": "3000"}, "portfolio_exposure_limit_exceeded"),
])
def test_rejects_invalid_or_over_limit_trade(changes, reason):
    result = RiskEngine().assess(request(**changes))
    assert not result.accepted
    assert reason in result.reasons


def test_round_trip_transaction_cost_and_net_reward_are_reported():
    result = RiskEngine().assess(request())
    assert result.round_trip_transaction_cost >= result.buy_transaction_cost
    assert result.gross_expected_reward == Decimal("18.00")
    assert result.net_expected_reward == result.gross_expected_reward - result.round_trip_transaction_cost
    assert result.transaction_costs_are_estimates


def test_stale_missing_naive_and_non_finite_data_are_rejected():
    engine = RiskEngine()
    assert "market_data_timestamp_missing" in engine.assess(request(market_data_at=None)).reasons
    assert "market_data_timestamp_invalid" in engine.assess(
        request(market_data_at=datetime(2026, 11, 15, 10))).reasons
    with pytest.raises(ValueError):
        engine.assess(request(entry_price="NaN"))


def test_leverage_derivatives_and_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        RiskConfig(leverage_enabled=True)
    with pytest.raises(ValueError):
        RiskConfig(derivatives_enabled=True)
    with pytest.raises(ValueError):
        RiskConfig(risk_per_trade=Decimal("1.1"))
    with pytest.raises(ValueError):
        FeeSchedule(stt_sell_rate=Decimal("-0.1"))


def test_kill_switch_persists_until_manual_deactivation(tmp_path):
    state_path = tmp_path / "nested" / "kill.json"
    control = KillSwitch(state_path)
    assert not control.is_active
    with pytest.raises(ValueError):
        control.activate(" ")
    control.activate("Daily loss review")
    assert control.is_active
    assert RiskEngine(kill_switch=control).assess(request()).reasons == ("kill_switch_active",)
    assert KillSwitch(state_path).is_active
    control.deactivate("Manual review completed")
    assert not control.is_active


def test_corrupt_kill_switch_state_fails_closed(tmp_path):
    path = tmp_path / "kill.json"
    path.write_text("not json", encoding="utf-8")
    assert KillSwitch(path).is_active
