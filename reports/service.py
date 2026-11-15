"""Source-separated paper trading and historical backtest reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import PaperOrders, Portfolio, Positions, Stock, TradeHistory

INDIA_TZ = ZoneInfo("Asia/Kolkata")
PAPER_RESULT = "PAPER_TRADING_RESULT"
BACKTEST_RESULT = "HISTORICAL_BACKTEST_RESULT"


@dataclass(frozen=True)
class ReportArtifact:
    """Tabular report plus explicit provenance and UTC creation time."""

    name: str
    result_type: str
    source_label: str
    generated_at: datetime
    data: pd.DataFrame
    summary: dict[str, Any] | None = None


class ReportingService:
    """Build analytics from the fill ledger or a single isolated backtest result."""

    def __init__(self, session: Session, *, portfolio_name: str = "Paper Portfolio") -> None:
        self.session = session
        self.portfolio_name = portfolio_name

    def paper_trades(self, *, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        query = (select(TradeHistory, Positions, Stock, PaperOrders)
                 .join(Positions, TradeHistory.position_id == Positions.id)
                 .join(Portfolio, Positions.portfolio_id == Portfolio.id)
                 .join(Stock, Positions.stock_id == Stock.id)
                 .outerjoin(PaperOrders, TradeHistory.paper_order_id == PaperOrders.id)
                 .where(TradeHistory.event_type == "SELL_FILL", Positions.status.in_(("open", "closed")),
                        Portfolio.name == self.portfolio_name)
                 .order_by(TradeHistory.created_at, TradeHistory.id))
        rows = []
        for fill, position, stock, order in self.session.execute(query):
            details = self._details(fill.details)
            local_day = fill.created_at.astimezone(INDIA_TZ).date()
            if start is not None and local_day < start or end is not None and local_day > end:
                continue
            buy_cost = Decimal(str(details.get("allocated_buy_fee", "0")))
            sell_cost = Decimal(str(details.get("sell_fee", "0")))
            slippage = Decimal(str(details.get("slippage_cost", "0")))
            risk_per_share = max(Decimal(position.average_entry_price) - Decimal(position.stop_loss or 0), Decimal("0"))
            risk_amount = risk_per_share * int(fill.quantity or 0)
            pnl = Decimal(str(details.get("realized_net_pnl", "0")))
            holding = max((fill.created_at - position.opened_at).total_seconds() / 86400, 0)
            strategy_version = "Manual / unlinked"
            if order is not None and order.approval is not None and order.approval.signal is not None:
                strategy_version = order.approval.signal.strategy_version
            observed_at = details.get("observed_at")
            rows.append({
                "trade_id": f"PAPER-{fill.id}", "position_id": position.id,
                "order_id": order.id if order else None, "symbol": stock.symbol,
                "entry_at_utc": position.opened_at.astimezone(timezone.utc).isoformat(),
                "exit_at_utc": fill.created_at.astimezone(timezone.utc).isoformat(),
                "trade_date_ist": local_day.isoformat(), "side": "LONG",
                "quantity_exited": int(fill.quantity or 0), "entry_price": float(position.average_entry_price),
                "exit_price": float(fill.price or 0), "gross_pnl_inr": float(details.get("gross_pnl", 0)),
                "net_pnl_inr": float(pnl), "buy_and_sell_charges_inr": float(buy_cost + sell_cost),
                "slippage_cost_inr": float(slippage), "risk_amount_inr": float(risk_amount),
                "realized_r_multiple": float(pnl / risk_amount) if risk_amount else None,
                "holding_period_days": holding, "exit_reason": details.get("reason", ""),
                "strategy_version": strategy_version,
                "paper_result_type": PAPER_RESULT,
                "market_data_classification": self._classify_source(str(details.get("source", "unknown"))),
                "market_data_source": str(details.get("source", "unknown")),
                "market_data_observed_at": observed_at or "",
            })
        return pd.DataFrame(rows, columns=self._paper_columns())

    def paper_analytics(self, *, today: date | None = None) -> dict[str, Any]:
        """Calculate separate daily, weekly, monthly and total paper-ledger metrics."""
        today = today or datetime.now(INDIA_TZ).date()
        all_trades = self.paper_trades()
        daily = all_trades[all_trades["trade_date_ist"] == today.isoformat()].copy()
        week_start = today - timedelta(days=today.weekday())
        weekly = all_trades[(all_trades["trade_date_ist"] >= week_start.isoformat()) &
                            (all_trades["trade_date_ist"] <= today.isoformat())].copy()
        month_start = today.replace(day=1)
        monthly = all_trades[(all_trades["trade_date_ist"] >= month_start.isoformat()) &
                             (all_trades["trade_date_ist"] <= today.isoformat())].copy()
        open_positions = list(self.session.scalars(select(Positions).join(Portfolio).where(
            Positions.status == "open", Portfolio.name == self.portfolio_name)))
        invested = sum((Decimal(p.average_entry_price) * p.quantity for p in open_positions), Decimal("0"))
        cash_delta = Decimal("0")
        for fill in self.session.scalars(select(TradeHistory).where(TradeHistory.event_type.in_(
                ("BUY_FILL", "SELL_FILL")))):
            details = self._details(fill.details)
            # Restrict cash to this named portfolio's positions.
            position = self.session.get(Positions, fill.position_id) if fill.position_id else None
            if position is not None and position.portfolio.name == self.portfolio_name:
                cash_delta += Decimal(str(details.get("cash_delta", "0")))
        portfolio = self.session.scalar(select(Portfolio).where(Portfolio.name == self.portfolio_name))
        initial = Decimal(portfolio.starting_capital) if portfolio else Decimal("3000")
        cash = initial + cash_delta
        closed_realized = float(all_trades["net_pnl_inr"].sum()) if not all_trades.empty else 0.0
        cumulative = all_trades["net_pnl_inr"].cumsum() if not all_trades.empty else pd.Series(dtype=float)
        equity = float(initial) + cumulative
        peak = equity.cummax() if len(equity) else pd.Series(dtype=float)
        max_dd = float(((equity / peak - 1) * 100).min()) if len(equity) else 0.0
        strategy = self.strategy_performance(all_trades)
        provenance = self.market_data_performance(all_trades)
        return {
            "generated_at": datetime.now(timezone.utc), "result_type": PAPER_RESULT,
            "paper_market_data_classes": sorted(all_trades["market_data_classification"].unique().tolist())
                if not all_trades.empty else [],
            "daily_pnl_inr": self._sum(daily, "net_pnl_inr"),
            "weekly_pnl_inr": self._sum(weekly, "net_pnl_inr"),
            "monthly_pnl_inr": self._sum(monthly, "net_pnl_inr"),
            "total_realized_pnl_inr": closed_realized,
            "total_return_pct": float(closed_realized / float(initial) * 100) if initial else 0.0,
            "invested_capital_inr": float(invested), "available_capital_inr": float(cash),
            "open_positions": len(open_positions), "win_rate_pct": self._win_rate(all_trades),
            "profit_factor": self._profit_factor(all_trades),
            "average_reward_risk": float(all_trades["realized_r_multiple"].mean())
                if all_trades["realized_r_multiple"].notna().any() else None,
            "maximum_drawdown_pct": max_dd,
            "average_holding_period_days": float(all_trades["holding_period_days"].mean())
                if not all_trades.empty else 0.0,
            "trading_costs_inr": float((all_trades["buy_and_sell_charges_inr"] +
                                         all_trades["slippage_cost_inr"]).sum())
                if not all_trades.empty else 0.0,
            "strategy_performance": strategy,
            "paper_performance_by_market_data": provenance,
            "unrealized_pnl_inr": None,
            "unrealized_note": "Unavailable without a validated current market quote; no price was inferred.",
        }

    def daily_report(self, report_date: date) -> ReportArtifact:
        return self._paper_artifact("Daily trading report", self.paper_trades(start=report_date, end=report_date))

    def weekly_report(self, week_ending: date) -> ReportArtifact:
        start = week_ending - timedelta(days=week_ending.weekday())
        return self._paper_artifact("Weekly performance report", self.paper_trades(start=start, end=week_ending))

    def monthly_report(self, month: date) -> ReportArtifact:
        start = month.replace(day=1)
        end = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return self._paper_artifact("Monthly performance report", self.paper_trades(start=start, end=end))

    def individual_trade_report(self, trade_id: str) -> ReportArtifact:
        frame = self.paper_trades()
        selected = frame[frame["trade_id"] == trade_id].copy()
        if selected.empty:
            raise LookupError(f"Paper trade {trade_id} was not found")
        return self._paper_artifact(f"Individual trade {trade_id}", selected)

    def strategy_report(self) -> ReportArtifact:
        trades = self.paper_trades()
        return self._paper_artifact("Strategy performance report", self.strategy_performance(trades))

    def risk_exposure_report(self) -> ReportArtifact:
        rows = []
        for position in self.session.scalars(select(Positions).join(Portfolio).where(
                Positions.status == "open", Portfolio.name == self.portfolio_name)):
            stock = self.session.get(Stock, position.stock_id)
            entry = Decimal(position.average_entry_price)
            risk = max(entry - Decimal(position.stop_loss or entry), Decimal("0")) * position.quantity
            rows.append({"position_id": position.id, "symbol": stock.symbol if stock else "UNKNOWN",
                         "quantity": position.quantity, "average_entry_price_inr": float(entry),
                         "invested_capital_inr": float(entry * position.quantity),
                         "stop_loss_inr": float(position.stop_loss) if position.stop_loss is not None else None,
                         "target_inr": float(position.target_price) if position.target_price is not None else None,
                         "risk_to_stop_inr": float(risk), "status": "OPEN_PAPER_POSITION",
                         "result_type": PAPER_RESULT, "market_data_classification": "MARKET_DATA_NOT_USED"})
        frame = pd.DataFrame(rows, columns=["position_id", "symbol", "quantity", "average_entry_price_inr",
            "invested_capital_inr", "stop_loss_inr", "target_inr", "risk_to_stop_inr", "status", "result_type"])
        return self._paper_artifact("Risk exposure report", frame)

    def backtest_report(self, result: Any) -> ReportArtifact:
        """Create a report from one backtest object without querying paper P&L."""
        trades = result.trades.copy()
        trades["result_type"] = BACKTEST_RESULT
        source_classification = self._classify_source(result.source)
        trades["market_data_classification"] = (source_classification if source_classification in {
            "LIVE_MARKET_DATA", "SIMULATED_MARKET_DATA"} else "HISTORICAL_MARKET_DATA")
        trades["market_data_source"] = result.source
        trades["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
        return ReportArtifact(f"{result.symbol} historical backtest", BACKTEST_RESULT,
                              result.source, datetime.now(timezone.utc), trades,
                              summary={**result.metrics.__dict__, "symbol": result.symbol,
                                       "strategy_version": result.strategy_version,
                                       "completed_through": result.completed_through.isoformat(),
                                       "slippage_bps": result.slippage_bps,
                                       "market_data_classification": trades["market_data_classification"].iloc[0]
                                           if not trades.empty else "HISTORICAL_MARKET_DATA"})

    def export_csv(self, artifact: ReportArtifact) -> bytes:
        frame = self._with_metadata(artifact)
        return frame.to_csv(index=False).encode("utf-8-sig")

    def export_excel(self, artifact: ReportArtifact) -> bytes:
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl", datetime_format="yyyy-mm-dd hh:mm:ss") as writer:
            metadata = pd.DataFrame([{"Report": artifact.name, "Result type": artifact.result_type,
                "Source": artifact.source_label, "Generated at (UTC)": artifact.generated_at.isoformat(),
                "Currency": "INR"}])
            metadata.to_excel(writer, index=False, sheet_name="Report metadata")
            self._with_metadata(artifact).to_excel(writer, index=False, sheet_name="Report data")
            if artifact.summary:
                pd.DataFrame([artifact.summary]).to_excel(writer, index=False, sheet_name="Performance summary")
            worksheet = writer.sheets["Report data"]
            headers = {cell.value: cell.column for cell in worksheet[1]}
            for header, column in headers.items():
                if isinstance(header, str) and header.lower().endswith("_inr"):
                    for row in worksheet.iter_rows(min_row=2, min_col=column, max_col=column):
                        row[0].number_format = '₹#,##0.00;[Red]-₹#,##0.00'
        return output.getvalue()

    def strategy_performance(self, trades: pd.DataFrame | None = None) -> pd.DataFrame:
        frame = trades if trades is not None else self.paper_trades()
        if frame.empty:
            return pd.DataFrame(columns=["strategy_version", "trades", "net_pnl_inr", "win_rate_pct", "profit_factor"])
        rows = []
        for key, group in frame.groupby("strategy_version", dropna=False):
            rows.append({"strategy_version": key, "trades": len(group),
                         "net_pnl_inr": float(group["net_pnl_inr"].sum()),
                         "win_rate_pct": self._win_rate(group), "profit_factor": self._profit_factor(group),
                         "result_type": PAPER_RESULT,
                         "market_data_classification": ", ".join(sorted(
                             group["market_data_classification"].dropna().unique().tolist()))})
        return pd.DataFrame(rows)

    def market_data_performance(self, trades: pd.DataFrame | None = None) -> pd.DataFrame:
        """Keep paper outcomes grouped by quote provenance; never label them live P&L."""
        frame = trades if trades is not None else self.paper_trades()
        columns = ["market_data_classification", "paper_trades", "paper_net_pnl_inr", "win_rate_pct"]
        if frame.empty:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame([{"market_data_classification": key, "paper_trades": len(group),
                              "paper_net_pnl_inr": float(group["net_pnl_inr"].sum()),
                              "win_rate_pct": self._win_rate(group), "result_type": PAPER_RESULT}
                             for key, group in frame.groupby("market_data_classification", dropna=False)])

    def _paper_artifact(self, name: str, frame: pd.DataFrame) -> ReportArtifact:
        return ReportArtifact(name, PAPER_RESULT, "SQLite paper fill ledger; execution results only",
                              datetime.now(timezone.utc), frame)

    @staticmethod
    def _with_metadata(artifact: ReportArtifact) -> pd.DataFrame:
        result = artifact.data.copy()
        result.insert(0, "report_generated_at_utc", artifact.generated_at.isoformat())
        result.insert(1, "report_result_type", artifact.result_type)
        result.insert(2, "report_source", artifact.source_label)
        if artifact.summary:
            for key, value in reversed(list(artifact.summary.items())):
                result.insert(3, f"summary_{key}", value)
        return result

    @staticmethod
    def _details(value: str | None) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _classify_source(source: str) -> str:
        normalized = source.strip().lower()
        if any(token in normalized for token in ("sim", "mock", "sample", "synthetic", "test")):
            return "SIMULATED_MARKET_DATA"
        if any(token in normalized for token in ("upstox", "live", "real_time", "realtime")):
            return "LIVE_MARKET_DATA"
        if any(token in normalized for token in ("historical", "backtest", "daily_candle", "ohlcv_csv")):
            return "HISTORICAL_MARKET_DATA"
        return "MARKET_DATA_SOURCE_UNCLASSIFIED"

    @staticmethod
    def _sum(frame: pd.DataFrame, column: str) -> float:
        return float(frame[column].sum()) if not frame.empty else 0.0

    @staticmethod
    def _win_rate(frame: pd.DataFrame) -> float:
        return float(frame["net_pnl_inr"].gt(0).mean() * 100) if not frame.empty else 0.0

    @staticmethod
    def _profit_factor(frame: pd.DataFrame) -> float | None:
        if frame.empty:
            return None
        wins = float(frame.loc[frame["net_pnl_inr"] > 0, "net_pnl_inr"].sum())
        losses = abs(float(frame.loc[frame["net_pnl_inr"] < 0, "net_pnl_inr"].sum()))
        return wins / losses if losses else (float("inf") if wins else None)

    @staticmethod
    def _paper_columns() -> list[str]:
        return ["trade_id", "position_id", "order_id", "symbol", "entry_at_utc", "exit_at_utc",
                "trade_date_ist", "side", "quantity_exited", "entry_price", "exit_price", "gross_pnl_inr",
                "net_pnl_inr", "buy_and_sell_charges_inr", "slippage_cost_inr", "risk_amount_inr",
                "realized_r_multiple", "holding_period_days", "exit_reason", "strategy_version",
                "paper_result_type", "market_data_classification", "market_data_source", "market_data_observed_at"]


def format_inr(value: float | Decimal | None) -> str:
    """Format an amount as Indian rupees, with explicit unavailable handling."""
    if value is None:
        return "N/A"
    return f"₹{float(value):,.2f}"
