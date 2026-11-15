"""Streamlit page renderers for the TradePilot research terminal."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from sqlalchemy import select
from database.models import (Approvals, MarketData, NotificationLogs,
                             PaperOrders, Positions, Stock,
                             TradeSignals)
from execution.paper_engine import DailyMarketBar, ExecutionQuote, PaperExecutionError, PaperTradingEngine
from execution.approval_manager import ApprovalManager
from notifications.telegram import TelegramNotifier
from backtesting.engine import BacktestConfig, HistoricalBacktester, InsufficientBacktestData
from backtesting.validation import run_chronological_train_test, walk_forward_test
from risk.engine import RiskConfig
from reports.service import ReportingService, format_inr

from config.settings import get_settings
from database.connection import SessionLocal
from frontend.components import metric_cards, page_header, render_source_badge
from frontend.api_client import ApiUnavailable, api_request
from frontend.services import TradingViewService
from integrations.upstox.instrument_master import InstrumentMasterClient
from market_data.universe import NIFTY50_SOURCE_PAGE, StockUniverseService
from scheduler.calendar import NSE_HOLIDAY_PAGE, import_official_calendar_csv
from strategy.scanner import filter_scan_results
from strategy.technical_analysis import TechnicalAnalysisEngine


def _intro(title: str, subtitle: str, service: TradingViewService, right: str | None = None) -> None:
    page_header(title, subtitle, right=right)
    state = service.repository_snapshot()
    stored = sum(state.record_counts.values()) if state.database_available else None
    if stored is None:
        st.caption(f"Repository unavailable ({state.error}); persisted figures are not available.")
    else:
        st.caption(f"Persisted SQLite repositories · {stored} stored records. Market-data provenance is shown per section.")


def _currency(value: float) -> str:
    return f"₹{value:,.2f}"


def render_dashboard() -> None:
    page_header("Dashboard Overview", "A live view of the local paper ledger and research pipeline.", right="PAPER ACCOUNT")
    trades = pd.DataFrame()
    snapshot = None
    try:
        with SessionLocal() as session:
            reports = ReportingService(session)
            paper_metrics = reports.paper_analytics()
            trades = reports.paper_trades()
            snapshot = PaperTradingEngine(session).portfolio_snapshot()
            today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
            daily_signal_count = len(list(session.scalars(select(TradeSignals.id).where(
                TradeSignals.signal_date == today_ist))))
        render_source_badge("SQLite paper fill ledger", "P&L and capital")
        book_value = snapshot.available_cash + snapshot.invested_capital
        metric_cards([
            ("PAPER EQUITY · AT COST", format_inr(book_value), "Cash + position cost basis"),
            ("AVAILABLE CASH", format_inr(snapshot.available_cash), "After recorded paper fills and charges"),
            ("INVESTED CAPITAL", format_inr(snapshot.invested_capital), "Open paper positions at average cost"),
            ("REALISED P&L", format_inr(snapshot.realized_pnl), "Closed paper fills after charges"),
            ("UNREALISED P&L", format_inr(snapshot.unrealized_pnl), "Only available with sourced current marks"),
            ("OPEN POSITIONS", str(snapshot.open_positions), "Local paper account"),
            ("TODAY'S SIGNALS", str(daily_signal_count), f"Stored signals · {today_ist.isoformat()} IST"),
            ("WIN RATE", f"{paper_metrics['win_rate_pct']:.1f}%", f"{len(trades)} paper exit fills"),
        ], columns=4)
        st.caption(paper_metrics["unrealized_note"])
    except Exception as exc:
        st.warning(f"Persisted paper analytics are unavailable ({type(exc).__name__}); the rest of the dashboard remains usable.")
    st.markdown("### Paper equity · realised ledger")
    if not trades.empty:
        plot = trades.sort_values(["exit_at_utc", "trade_id"]).copy()
        plot["equity_inr"] = float(snapshot.initial_capital) + plot["net_pnl_inr"].cumsum()
        fig = go.Figure(go.Scatter(x=plot["exit_at_utc"], y=plot["equity_inr"], mode="lines+markers",
                                   line=dict(color="#A78BFA", width=2.5), fill="tozeroy",
                                   fillcolor="rgba(139,92,246,.12)", name="Paper equity"))
        fig.update_layout(height=320, margin=dict(l=5, r=5, t=15, b=10), paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)", font_color="#A1A1AA", xaxis_title="Exit time · UTC",
                          yaxis_title="Equity · INR", hovermode="x unified")
        fig.update_yaxes(gridcolor="#303047")
        st.plotly_chart(fig, use_container_width=True, theme=None)
    else:
        st.info("The paper equity chart will appear after the first recorded exit. No synthetic account curve is substituted.")
    _render_scheduler_status()
    st.markdown("### Stored strategy BUY opportunities")
    st.caption("Strategy conditions have passed. Each candidate still needs a fresh sourced quote and current risk validation on Trade Approval before it can be approved.")
    try:
        with SessionLocal() as session:
            ApprovalManager(session).expire_due()
            pending = list(session.scalars(select(TradeSignals).where(
                TradeSignals.signal_type == "BUY", TradeSignals.status.notin_(
                    ("rejected", "expired", "approved", "risk_rejected")))
                .order_by(TradeSignals.created_at.desc()).limit(10)))
            st.dataframe([{"Symbol": item.stock.symbol, "Signal": item.signal_key,
                           "Signal date": item.signal_date, "Reference price": item.reference_price,
                           "Stop": item.stop_loss, "Target": item.target_price,
                           "Expires": item.expires_at, "Strategy version": item.strategy_version}
                          for item in pending], hide_index=True, use_container_width=True,
                         column_config={"Reference price": st.column_config.NumberColumn(format="₹%.2f"),
                                        "Stop": st.column_config.NumberColumn(format="₹%.2f"),
                                        "Target": st.column_config.NumberColumn(format="₹%.2f")})
            if not pending:
                st.info("No stored BUY candidates are currently awaiting review.")
    except Exception as exc:
        st.error(f"Unable to load stored strategy candidates: {exc}")


def _render_scheduler_status() -> None:
    st.markdown("### Daily scheduler status")
    payload = None
    try:
        with st.spinner("Checking scheduler status…"):
            payload = api_request("GET", "/scheduler/status", timeout=5).json()
    except ApiUnavailable as exc:
        st.warning(f"Scheduler status unavailable: {exc}")

    if payload is not None:
        calendar = payload.get("calendar", {})
        state_col, tz_col, cal_col = st.columns(3)
        state_col.metric("SCHEDULER", payload.get("state", "unknown").upper(),
                         "Background APScheduler service")
        tz_col.metric("MARKET TIMEZONE", payload.get("timezone", "unknown"),
                      payload.get("local_time", ""))
        cal_col.metric("NSE CALENDAR", "VERIFIED" if calendar.get("verified") else "MISSING",
                       f"{calendar.get('year', '')} · {calendar.get('exception_count', 0)} exceptions")
        if calendar.get("source_url"):
            st.caption(f"Calendar: {calendar.get('source_name')} · source refreshed UTC: "
                       f"{calendar.get('source_refreshed_at')} · {calendar.get('source_url')}")
        if payload.get("start_error"):
            st.error(f"Scheduler startup failed ({payload['start_error']}); it can be retried by restarting the API. Manual application use is unaffected.")
        if payload.get("jobs"):
            st.markdown("**Next scheduled jobs (UTC)**")
            st.dataframe(payload["jobs"], hide_index=True, use_container_width=True)
        if payload.get("recent_runs"):
            st.markdown("**Recent scheduled operations**")
            st.dataframe([{"Operation": row["job_name"], "Market date": row["market_date"],
                           "Status": row["status"], "Attempts": row["attempts"],
                           "Started UTC": row["started_at"], "Finished UTC": row["finished_at"],
                           "Error": row["error_type"], "Details": row["details"]}
                          for row in payload["recent_runs"]], hide_index=True, use_container_width=True)

    with st.expander("Import an updated official NSE calendar"):
        st.caption("Import the equity holiday and special-session exceptions published by NSE. Unknown years are treated as non-trading until a calendar is verified.")
        with st.form("nse_calendar_import"):
            calendar_file = st.file_uploader("Calendar CSV · date, kind, description, opens_at, closes_at", type=["csv"])
            source_url = st.text_input("Official NSE source URL", value=NSE_HOLIDAY_PAGE)
            source_name = st.text_input("Calendar publication or circular name", value="NSE Equities trading calendar")
            import_clicked = st.form_submit_button("Import verified calendar")
        if import_clicked:
            if calendar_file is None:
                st.error("Select the NSE calendar CSV first.")
            else:
                try:
                    with SessionLocal() as session:
                        year = import_official_calendar_csv(session,
                            calendar_file.getvalue().decode("utf-8-sig"), source_url=source_url,
                            source_name=source_name)
                    st.success(f"Imported NSE calendar exceptions for {year}. Restart the backend to register timed special-session monitoring jobs.")
                    st.rerun()
                except (UnicodeDecodeError, ValueError, OSError) as exc:
                    st.error(f"Calendar import failed: {exc}")


def render_market() -> None:
    service = TradingViewService()
    _intro("Market Overview", "Exchange breadth calculated only from persisted NSE daily candles.", service, "STORED MARKET DATA")
    with SessionLocal() as session:
        stocks = list(session.scalars(select(Stock).where(Stock.in_nifty50.is_(True), Stock.active.is_(True))
                                      .order_by(Stock.symbol)))
        rows = []
        for stock in stocks:
            bars = list(session.scalars(select(MarketData).where(
                MarketData.stock_id == stock.id, MarketData.interval.in_(("1d", "day", "daily")))
                .order_by(MarketData.candle_at.desc()).limit(2)))
            if not bars:
                continue
            latest = bars[0]
            previous = bars[1] if len(bars) > 1 else None
            change = ((float(latest.close) / float(previous.close) - 1) * 100
                      if previous is not None and float(previous.close) else None)
            rows.append({"Symbol": stock.symbol, "Company": stock.name, "Market date": latest.market_date,
                         "Close · ₹": float(latest.close), "Daily change · %": change,
                         "Volume": latest.volume, "Source": latest.source})
    frame = pd.DataFrame(rows)
    if frame.empty:
        metric_cards([("NIFTY 50 TRACKED", str(len(stocks)), "Enabled constituents"),
                      ("DAILY CANDLES", "0", "Persisted NSE records"),
                      ("ADVANCING", "N/A", "Requires two stored sessions"),
                      ("DECLINING", "N/A", "Requires two stored sessions")], columns=4)
        st.info("No persisted daily market candles are available yet. Refresh the official universe and authorize the read-only Upstox market-data flow from the NIFTY 50 Scanner. Index values and market breadth are not estimated.")
        return
    advances = int(frame["Daily change · %"].gt(0).sum())
    declines = int(frame["Daily change · %"].lt(0).sum())
    unchanged = int(frame["Daily change · %"].eq(0).sum())
    latest_time = frame["Market date"].max()
    sources = ", ".join(sorted(frame["Source"].dropna().astype(str).unique()))
    metric_cards([("NIFTY 50 TRACKED", str(len(stocks)), "Enabled constituents"),
                  ("DAILY CANDLES", str(len(frame)), f"Latest market date {latest_time}"),
                  ("ADVANCING", str(advances), "Against prior stored daily close"),
                  ("DECLINING", str(declines), f"Unchanged: {unchanged}")], columns=4)
    render_source_badge(sources, "persisted daily candles")
    left, right = st.columns([1.2, 1])
    with left:
        st.markdown("### NIFTY 50 daily snapshot")
        search = st.text_input("Search symbol or company", key="market_search")
        visible = frame
        if search.strip():
            mask = frame["Symbol"].str.contains(search.strip(), case=False, na=False) | frame["Company"].str.contains(search.strip(), case=False, na=False)
            visible = frame.loc[mask]
        st.dataframe(visible, hide_index=True, use_container_width=True,
                     column_config={"Close · ₹": st.column_config.NumberColumn(format="₹%.2f"),
                                    "Daily change · %": st.column_config.NumberColumn(format="%.2f%%")})
    with right:
        st.markdown("### Constituents by daily direction")
        fig = go.Figure(go.Bar(x=["Advancing", "Unchanged", "Declining"],
                               y=[advances, unchanged, declines],
                               marker_color=["#22C55E", "#71717A", "#EF4444"],
                               hovertemplate="%{x}: %{y}<extra></extra>"))
        fig.update_layout(height=320, margin=dict(l=5, r=5, t=15, b=5), paper_bgcolor="rgba(0,0,0,0)",
                          plot_bgcolor="rgba(0,0,0,0)", font_color="#A1A1AA", yaxis_title="Stored constituents",
                          hovermode="x")
        fig.update_yaxes(gridcolor="#303047")
        st.plotly_chart(fig, use_container_width=True, theme=None)


def render_scanner() -> None:
    service = TradingViewService()
    _intro("NIFTY 50 Scanner", "Current official constituents, validated NSE equity mappings, and daily-candle research scan.", service, "READ ONLY")
    with SessionLocal() as session:
        universe = StockUniverseService(session)
        status = universe.status()
        st.caption(f"Constituent source: {status.source or 'Not loaded'} · last checked/imported (UTC): "
                   f"{status.updated_at.isoformat() if status.updated_at else 'Not available'} · "
                   f"NIFTY 50 members: {status.constituent_count} · enabled: {status.enabled_count} · "
                   f"{'STALE — refresh before treating as current' if status.stale else 'recently checked'}")
        st.markdown(f"Official source: [{NIFTY50_SOURCE_PAGE}]({NIFTY50_SOURCE_PAGE})")
        refresh_col, upload_col = st.columns([1, 2])
        if refresh_col.button("Refresh official NIFTY 50", type="primary"):
            try:
                result = universe.refresh_official()
                st.success(f"Refreshed official list. Added {len(result.added)}, removed {len(result.removed)}, "
                           f"unchanged {len(result.unchanged)}; unavailable mappings: {len(result.unavailable)}.")
                if result.unavailable:
                    st.warning("Unavailable Upstox instruments: " + ", ".join(result.unavailable))
                st.rerun()
            except Exception as exc:
                st.error(f"Constituent refresh failed ({type(exc).__name__}). Existing database membership was retained.")
        uploaded = upload_col.file_uploader("Import constituent CSV", type=["csv"], key="nifty_constituents_upload")
        if uploaded is not None and upload_col.button("Import CSV", key="import_constituents"):
            try:
                imported = universe.import_csv_text(uploaded.getvalue().decode("utf-8-sig"),
                                                    source=f"CSV import: {uploaded.name}")
                st.success(f"Imported {len(imported.added) + len(imported.unchanged)} symbols; "
                           f"unavailable mappings: {len(imported.unavailable)}.")
                st.rerun()
            except (UnicodeDecodeError, ValueError, OSError) as exc:
                st.error(f"CSV import failed: {exc}")
        st.download_button("Export NIFTY 50 CSV", universe.export_csv(), "tradepilot_nifty50.csv", "text/csv")

        manual_col, control_col = st.columns([2, 2])
        manual_symbol = manual_col.text_input("Add manual NSE watchlist symbol", placeholder="e.g. NSE symbol")
        if manual_col.button("Add to manual watchlist", disabled=not manual_symbol.strip()):
            try:
                added = universe.add_manual(manual_symbol, instrument_master=InstrumentMasterClient())
                st.success(f"Added {added.symbol} to the manual watchlist using its validated NSE equity mapping.")
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(str(exc))
        stocks = universe.list_stocks()
        if stocks:
            selected = control_col.selectbox("Stock to enable / disable", stocks, format_func=lambda item: f"{item.symbol} · {'enabled' if item.active else 'disabled'}")
            enable_value = not selected.active
            if control_col.button("Enable selected" if enable_value else "Disable selected", disabled=enable_value and not selected.instrument_available):
                universe.set_enabled(selected.symbol, enable_value)
                st.rerun()
            table = pd.DataFrame([{"Symbol": item.symbol, "Company": item.name, "NIFTY 50": item.in_nifty50,
                                   "Enabled": item.active, "Instrument available": item.instrument_available,
                                   "Instrument key": item.instrument_key or "Unavailable"} for item in stocks])
            st.dataframe(table, hide_index=True, use_container_width=True)
        else:
            st.info("Load the official list or import a CSV to begin managing the universe.")

        if st.button("Run daily scanner", type="primary", disabled=not any(s.active and s.instrument_available for s in stocks)):
            try:
                with st.spinner("Retrieving completed daily candles and scanning eligible constituents…"):
                    result = api_request("GET", "/universe/scanner", timeout=300).json()
                st.session_state["nifty_scan_results"] = result["results"]
                st.session_state["nifty_scan_source"] = result["source"]
            except ApiUnavailable as exc:
                st.error(f"Scanner service unavailable: {exc}. Complete Upstox authorization if needed.")

    raw_results = st.session_state.get("nifty_scan_results")
    if raw_results:
        st.caption(f"Scan data source: {st.session_state.get('nifty_scan_source', 'Upstox Historical Candle Data V3')} · daily historical candles")
        # Use shared typed scanner filter semantics by reconstructing the result records returned by the API.
        from strategy.scanner import ScannerResult
        results = [ScannerResult(**row) for row in raw_results]
        filters = st.columns(4)
        direction = filters[0].selectbox("Signal", ["All", "Bullish", "Bearish", "Neutral"])
        ema = filters[1].selectbox("EMA trend", ["All", "Bullish", "Bearish", "Mixed"])
        strong_volume = filters[2].checkbox("Strong volume only (≥1.5×)")
        minimum_rr = filters[3].number_input("Minimum risk/reward", min_value=0.0, max_value=10.0, value=0.0, step=0.25)
        numeric_filters = st.columns(3)
        rsi_min = numeric_filters[0].number_input("RSI minimum", min_value=0.0, max_value=100.0, value=0.0)
        rsi_max = numeric_filters[1].number_input("RSI maximum", min_value=0.0, max_value=100.0, value=100.0)
        sort = numeric_filters[2].selectbox("Sort by", ["symbol", "close", "rsi14", "volume_ratio", "risk_reward"])
        descending = st.checkbox("Descending", value=True, key="scanner_descending")
        try:
            filtered = filter_scan_results(results, direction=direction, strong_volume_only=strong_volume,
                                           rsi_min=rsi_min, rsi_max=rsi_max, ema_trend=ema,
                                           minimum_risk_reward=minimum_rr, sort_by=sort, descending=descending)
        except ValueError as exc:
            st.error(str(exc))
            filtered = []
        ready = [row for row in filtered if row.status == "Ready"]
        table = pd.DataFrame([{"Symbol": row.symbol, "Company": row.company_name, "Close": row.close,
                                   "EMA 20": row.ema20, "EMA 50": row.ema50, "RSI 14": row.rsi14,
                                   "Volume ratio": row.volume_ratio, "Signal": row.direction,
                                   "EMA trend": row.ema_trend, "Risk/reward": row.risk_reward,
                                   "Research stop": row.planned_stop, "Research target": row.planned_target,
                                   "Candles": row.candle_count, "Retrieved at (UTC)": row.retrieved_at}
                                  for row in ready])
        symbol_query = st.text_input("Search scanned symbol or company", key="scanner_search")
        if symbol_query.strip() and not table.empty:
            mask = table["Symbol"].str.contains(symbol_query.strip(), case=False, na=False) | table["Company"].str.contains(symbol_query.strip(), case=False, na=False)
            table = table.loc[mask]
        if table.empty:
            st.info("No ready stocks match the current scanner filters.")
        else:
            st.dataframe(table, hide_index=True, use_container_width=True,
                         column_config={"Close": st.column_config.NumberColumn(format="₹%.2f"),
                                        "EMA 20": st.column_config.NumberColumn(format="₹%.2f"),
                                        "EMA 50": st.column_config.NumberColumn(format="₹%.2f"),
                                        "RSI 14": st.column_config.NumberColumn(format="%.2f"),
                                        "Volume ratio": st.column_config.NumberColumn(format="%.2f"),
                                        "Research stop": st.column_config.NumberColumn(format="₹%.2f"),
                                        "Research target": st.column_config.NumberColumn(format="₹%.2f")})
        failed = [row for row in results if row.status != "Ready"]
        if failed:
            st.markdown("#### Unavailable / incomplete scan results")
            st.dataframe(pd.DataFrame([{"Symbol": row.symbol, "Status": row.status, "Candles": row.candle_count,
                                       "Explanation": row.explanation} for row in failed]),
                         hide_index=True, use_container_width=True)


def _candlestick_figure(frame: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=.035,
                        row_heights=[.62, .18, .20], subplot_titles=("NSE daily OHLC", "RSI 14", "Volume · shares"))
    x = frame.index
    fig.add_trace(go.Candlestick(x=x, open=frame["open"], high=frame["high"], low=frame["low"], close=frame["close"],
                                 increasing_line_color="#22C55E", decreasing_line_color="#EF4444", name="OHLC"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=frame["ema20"], line=dict(color="#A78BFA", width=1.5), name="EMA 20"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=frame["ema50"], line=dict(color="#60A5FA", width=1.5), name="EMA 50"), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=frame["rsi14"], line=dict(color="#FBBF24", width=1.5), name="RSI 14"), row=2, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="#EF4444", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#22C55E", row=2, col=1)
    colors = ["#22C55E" if close >= open_ else "#EF4444" for open_, close in zip(frame["open"], frame["close"])]
    fig.add_trace(go.Bar(x=x, y=frame["volume"], marker_color=colors, name="Volume"), row=3, col=1)
    fig.update_layout(height=680, xaxis_rangeslider_visible=False, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", font_color="#A1A1AA", margin=dict(l=10, r=10, t=45, b=10),
                      legend=dict(orientation="h", y=1.04), hovermode="x unified")
    fig.update_xaxes(showgrid=False, title_text="Market date", row=3, col=1)
    fig.update_yaxes(gridcolor="#303047")
    return fig


def render_stock_analysis() -> None:
    service = TradingViewService()
    _intro("Stock Analysis", "Interactive technical analysis of stored NSE daily candles.", service, "STORED OHLCV")
    with SessionLocal() as session:
        stocks = list(session.scalars(select(Stock).where(Stock.active.is_(True)).order_by(Stock.symbol)))
        if not stocks:
            st.info("No enabled stocks are stored yet. Load the official universe from the NIFTY 50 Scanner.")
            return
        stock = st.selectbox("NSE stock", stocks, format_func=lambda item: f"{item.symbol} · {item.name}",
                             help="Prices and indicators below use only persisted daily candles for this stock.")
        rows = list(session.scalars(select(MarketData).where(
            MarketData.stock_id == stock.id, MarketData.interval.in_(("1d", "day", "daily")))
            .order_by(MarketData.candle_at).limit(1000)))
        if not rows:
            st.info(f"No persisted daily candles are stored for {stock.symbol}. No sample price series is substituted.")
            st.caption("Use the NIFTY 50 Scanner after setting up Upstox read-only authorization and verified instrument mappings.")
            return
        raw = pd.DataFrame([{"open": float(item.open), "high": float(item.high), "low": float(item.low),
                             "close": float(item.close), "volume": item.volume}
                            for item in rows], index=pd.DatetimeIndex([item.candle_at for item in rows], name="timestamp"))
        source = ", ".join(sorted({item.source for item in rows}))
        render_source_badge(source, f"{stock.symbol} persisted daily candles")
        if len(raw) < 51:
            st.warning(f"{len(raw)} stored candle(s) are available. At least 51 completed sessions are needed to calculate the configured EMA 50 and technical view.")
            st.dataframe(raw.tail(20), use_container_width=True)
            return
        analysis = TechnicalAnalysisEngine().analyze(raw, end_of_day=False, source=source)
        latest = analysis.latest
        metric_cards([
            ("LATEST CLOSE", format_inr(latest.close), f"{stock.symbol} · {latest.timestamp.date()}"),
            ("EMA 20", format_inr(latest.ema20), "Exponential moving average"),
            ("EMA 50", format_inr(latest.ema50), "Exponential moving average"),
            ("RSI 14", f"{latest.rsi14:.2f}", "Momentum · 0–100"),
            ("ATR 14", format_inr(latest.atr14), "Average true range"),
            ("RELATIVE VOLUME", f"{latest.relative_volume:.2f}×", "Prior 20-session volume mean"),
        ], columns=3)
        st.plotly_chart(_candlestick_figure(analysis.frame), use_container_width=True, theme=None)
        signal_rows = list(session.scalars(select(TradeSignals).where(
            TradeSignals.stock_id == stock.id).order_by(TradeSignals.created_at.desc()).limit(50)))
        st.markdown("### Persisted strategy signal history")
        if signal_rows:
            st.dataframe([{"Signal date": item.signal_date, "Type": item.signal_type,
                           "Reference · ₹": float(item.reference_price) if item.reference_price is not None else None,
                           "Stop · ₹": float(item.stop_loss) if item.stop_loss is not None else None,
                           "Target · ₹": float(item.target_price) if item.target_price is not None else None,
                           "Status": item.status, "Version": item.strategy_version,
                           "Explanation": item.rationale} for item in signal_rows],
                         hide_index=True, use_container_width=True,
                         column_config={"Reference · ₹": st.column_config.NumberColumn(format="₹%.2f"),
                                        "Stop · ₹": st.column_config.NumberColumn(format="₹%.2f"),
                                        "Target · ₹": st.column_config.NumberColumn(format="₹%.2f")})
        else:
            st.info("No persisted strategy signals exist for this stock.")


def render_signals() -> None:
    service = TradingViewService()
    _intro("Trading Signals", "Persisted strategy outputs and their current approval lifecycle.", service, "APPROVAL REQUIRED")
    try:
        with SessionLocal() as session:
            signals = list(session.scalars(select(TradeSignals).order_by(TradeSignals.created_at.desc()).limit(250)))
            if not signals:
                st.info("No strategy signals are stored. Generate them from the Trading Signals API after authorizing read-only market data.")
                return
            statuses = sorted({item.status for item in signals})
            selected_status = st.multiselect("Show status", statuses, default=statuses)
            search = st.text_input("Search signal symbol or key", key="signal_search")
            rows = [{"Created (UTC)": item.created_at, "NSE symbol": item.stock.symbol,
                     "Signal": item.signal_type, "Signal date": item.signal_date,
                     "Reference price": item.reference_price, "Stop-loss": item.stop_loss,
                     "Target": item.target_price, "Risk/reward": item.risk_reward,
                     "Expires": item.expires_at, "Status": item.status,
                     "Strategy version": item.strategy_version,
                     "Approval": item.approval.status if item.approval else "Not reviewed"}
                    for item in signals if item.status in selected_status and
                    (not search.strip() or search.strip().casefold() in item.stock.symbol.casefold() or
                     search.strip().casefold() in (item.signal_key or "").casefold())]
            if rows:
                st.dataframe(rows, hide_index=True, use_container_width=True,
                             column_config={"Reference price": st.column_config.NumberColumn(format="₹%.2f"),
                                            "Stop-loss": st.column_config.NumberColumn(format="₹%.2f"),
                                            "Target": st.column_config.NumberColumn(format="₹%.2f"),
                                            "Risk/reward": st.column_config.NumberColumn(format="%.2f")})
            else:
                st.info("No stored signals match the selected status and search filters.")
            st.caption("Stored signal values come from the strategy snapshot. Approval requires a new fresh quote and re-run of the deterministic risk checks.")
    except Exception as exc:
        st.error(f"Unable to load persisted strategy signals: {exc}")


def render_approvals() -> None:
    service = TradingViewService()
    _intro("Trade Approval", "Risk-check each stored BUY signal, review the full paper plan, then make a recorded decision.", service, "EXPLICIT CONFIRMATION")
    st.warning("Approval is paper-only. Supply a real, fresh quote and its source; no sample or stale price is accepted. Telegram alerts cannot approve trades.")
    try:
        with SessionLocal() as session:
            ApprovalManager(session).expire_due()
            candidates = list(session.scalars(select(TradeSignals).where(
                TradeSignals.signal_type == "BUY", TradeSignals.status.notin_(
                    ("rejected", "expired", "risk_rejected")))
                .order_by(TradeSignals.created_at.desc()).limit(100)))
            open_reviews = [item for item in session.scalars(select(Approvals).where(Approvals.status == "pending")
                            .order_by(Approvals.created_at.desc()))]
            choices = {f"{item.stock.symbol} · {item.signal_key or item.id} · expires {item.expires_at or 'unknown'}": item
                       for item in candidates if item.approval is None and
                       (item.expires_at is None or item.expires_at > datetime.now(timezone.utc))}
            if choices:
                st.markdown("### New BUY opportunities")
                labels = list(choices)
                with st.form("approval_review_form"):
                    label = st.selectbox("Strategy signal", labels)
                    price = st.number_input("Current quote · ₹", min_value=0.0, value=0.0, format="%.4f")
                    source = st.text_input("Quote source or provider")
                    quote_time = st.text_input("Quote timestamp (ISO 8601 with timezone)", placeholder="2026-11-15T15:30:00+05:30")
                    review_now = st.form_submit_button("Risk-check and open trade details")
                if review_now:
                    try:
                        observed_at = datetime.fromisoformat(quote_time.strip())
                        item = choices[label]
                        review = ApprovalManager(session).review(item.signal_key, ExecutionQuote(
                            price, source, observed_at))
                        st.session_state["tradepilot_active_approval"] = review.approval_id
                        st.success("Risk checks passed. Review this persisted signal snapshot below.")
                    except (ValueError, LookupError, PaperExecutionError) as exc:
                        st.error(str(exc))

            active_id = st.session_state.get("tradepilot_active_approval")
            active = session.get(Approvals, active_id) if active_id else None
            if active is None and open_reviews:
                active = open_reviews[0]
            if active is not None and active.status == "pending":
                signal = active.signal
                st.markdown("### Review trade details")
                metric_cards([
                    ("STOCK", signal.stock.symbol, "NSE equity"),
                    ("ENTRY", f"₹{active.decision_entry_price:,.2f}", "Reviewed quote"),
                    ("QUANTITY", str(active.approved_quantity), "Maximum risk-qualified size"),
                    ("STOP-LOSS", f"₹{active.decision_stop_loss:,.2f}", "From stored strategy signal"),
                    ("TARGET", f"₹{active.decision_target_price:,.2f}", "From stored strategy signal"),
                    ("RISK", f"₹{active.risk_amount:,.2f}", "Includes estimated round-trip charges"),
                    ("EST. CHARGES", f"₹{active.estimated_charges:,.2f}", "Estimate; not broker quote"),
                ], columns=4)
                st.caption(f"Signal version: {active.signal_version} · Decision is tied to this version.")
                st.caption(f"Reviewed quote: {active.market_data_source or 'unknown'} · "
                           f"{active.market_data_at.isoformat() if active.market_data_at else 'timestamp unavailable'}")
                st.write(signal.rationale or "No explanation was saved with this signal.")
                with st.form(f"decision_{active.id}"):
                    decision = st.radio("Decision", ["Reject", "Approve"], horizontal=True)
                    quantity = st.number_input("Approved quantity", min_value=1,
                                               max_value=max(1, int(active.approved_quantity or 1)),
                                               value=max(1, int(active.approved_quantity or 1)), step=1)
                    confirm_text = st.text_input(f"Type {decision.upper()} {signal.signal_key} to confirm")
                    fresh_price = st.number_input("Confirm with a fresh current quote · ₹", min_value=0.0,
                                                   value=0.0, format="%.4f")
                    fresh_source = st.text_input("Fresh quote source", key=f"decision_source_{active.id}")
                    fresh_time = st.text_input("Fresh quote timestamp (ISO 8601 with timezone)",
                                               placeholder="2026-11-15T15:30:00+05:30", key=f"decision_time_{active.id}")
                    comment = st.text_input("Decision note")
                    submit_decision = st.form_submit_button("Record decision")
                if submit_decision:
                    try:
                        quote = None
                        if decision == "Approve":
                            quote = ExecutionQuote(fresh_price, fresh_source,
                                                   datetime.fromisoformat(fresh_time.strip()))
                        approval, order_id = ApprovalManager(session).decide(
                            active.id, decision=decision.lower(), expected_signal_version=active.signal_version,
                            confirm_text=confirm_text, quote=quote, quantity=int(quantity),
                            actor="local-user", comment=comment)
                        st.session_state.pop("tradepilot_active_approval", None)
                        if order_id:
                            st.success(f"Approval recorded at {approval.decision_at}. Paper order #{order_id} is approved and awaits a sourced simulated fill.")
                        else:
                            st.info(f"Decision recorded: {approval.status}.")
                    except (ValueError, LookupError, PaperExecutionError) as exc:
                        st.error(str(exc))
            elif not choices and not open_reviews:
                st.success("No unreviewed BUY signals are awaiting approval.")
            if open_reviews:
                st.markdown("### Pending reviews")
                st.dataframe([{"Approval": item.id, "Stock": item.signal.stock.symbol,
                               "Entry": float(item.decision_entry_price or 0),
                               "Quantity": item.approved_quantity, "Status": item.status,
                               "Signal version": item.signal_version} for item in open_reviews],
                             use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Unable to load persisted approval workflow: {exc}")


@st.dialog("Confirm paper order decision")
def _confirm_pending_paper_order() -> None:
    """Require an explicit dialog confirmation before changing an order state."""
    decision = st.session_state.get("pending_paper_order_decision")
    if not decision:
        st.info("No paper order action is pending.")
        return
    order_id, action = decision["order_id"], decision["action"]
    with SessionLocal() as session:
        order = session.get(PaperOrders, order_id)
        if order is None or order.status != "PENDING":
            st.error("This order is no longer pending. Refresh the page before taking another action.")
            if st.button("Close", key="close-stale-order-dialog"):
                st.session_state.pop("pending_paper_order_decision", None)
                st.rerun()
            return
        st.warning(f"Confirm {action.lower()} of paper order #{order.id}: {order.side} {order.quantity} share(s) of {order.stock.symbol if order.stock else order.stock_id}?")
        st.caption("This changes only the local paper-order record. No Upstox order will be sent.")
        confirm, cancel = st.columns(2)
        if confirm.button(f"Confirm {action}", type="primary", key=f"confirm-paper-order-{order.id}"):
            try:
                engine = PaperTradingEngine(session)
                if action == "Approve":
                    engine.approve_order(order.id, approved_by="local-user")
                else:
                    engine.reject_order(order.id, reason="Rejected in local paper interface")
                st.session_state.pop("pending_paper_order_decision", None)
                st.success(f"Paper order #{order.id} {action.lower()}d.")
                st.rerun()
            except (ValueError, LookupError, PaperExecutionError) as exc:
                st.error(str(exc))
        if cancel.button("Cancel", key=f"cancel-paper-order-{order.id}"):
            st.session_state.pop("pending_paper_order_decision", None)
            st.rerun()


def render_paper_trading() -> None:
    if st.session_state.get("pending_paper_order_decision"):
        _confirm_pending_paper_order()
    service = TradingViewService()
    _intro("Paper Trading", "Approval-based simulated fills persisted in the local SQLite portfolio.", service, "PAPER ONLY · NO BROKER ORDERS")
    st.warning("All prices entered here must come from a source you trust. This page never sends orders to Upstox.")
    try:
        with SessionLocal() as session:
            engine = PaperTradingEngine(session)
            snapshot = engine.portfolio_snapshot()
            orders = list(session.scalars(select(PaperOrders).order_by(PaperOrders.created_at.desc()).limit(100)))
            positions = list(session.scalars(select(Positions).where(Positions.status == "open")))

            metric_cards([
                ("AVAILABLE CASH", f"₹{snapshot.available_cash:,.2f}", "Persistent paper ledger"),
                ("INVESTED CAPITAL", f"₹{snapshot.invested_capital:,.2f}", "Open positions at average cost"),
                ("REALISED P&L", f"₹{snapshot.realized_pnl:,.2f}", "After recorded charges"),
                ("UNREALISED P&L", f"₹{snapshot.unrealized_pnl:,.2f}" if snapshot.unrealized_pnl is not None else "Not marked", "Requires current prices"),
            ], columns=4)

            st.info("New BUY orders are created only after the Trade Approval workflow records a versioned human decision. Review stored signals from the Trade Approval page.")

            orders = list(session.scalars(select(PaperOrders).order_by(PaperOrders.created_at.desc()).limit(100)))
            with st.expander("3 · Review and approve paper orders"):
                pending = [item for item in orders if item.status == "PENDING"]
                if not pending:
                    st.info("No pending paper orders.")
                else:
                    for order in pending:
                        left, approve_col, reject_col = st.columns([4, 1, 1])
                        left.write(f"#{order.id} · {order.side} · {order.quantity} · {order.stock.symbol if order.stock else order.stock_id} · PENDING")
                        if approve_col.button("Approve", key=f"paper-approve-{order.id}"):
                            st.session_state["pending_paper_order_decision"] = {"order_id": order.id, "action": "Approve"}
                            st.rerun()
                        if reject_col.button("Reject", key=f"paper-reject-{order.id}"):
                            st.session_state["pending_paper_order_decision"] = {"order_id": order.id, "action": "Reject"}
                            st.rerun()

            orders = list(session.scalars(select(PaperOrders).order_by(PaperOrders.created_at.desc()).limit(100)))
            fillable = [item for item in orders if item.status in {"APPROVED", "PARTIALLY_FILLED"}]
            with st.expander("4 · Simulate a fill from a sourced quote"):
                if not fillable:
                    st.info("No approved orders are awaiting fills.")
                else:
                    choices = {f"#{item.id} {item.side} {item.stock.symbol if item.stock else item.stock_id} · remaining {item.quantity-item.filled_quantity}": item.id for item in fillable}
                    with st.form("paper_fill_form"):
                        chosen_order = st.selectbox("Approved order", list(choices))
                        last = st.number_input("Observed last price · ₹", min_value=0.0, value=0.0, format="%.4f")
                        bid_value = st.number_input("Bid · optional, 0 to omit", min_value=0.0, value=0.0, format="%.4f")
                        ask_value = st.number_input("Ask · optional, 0 to omit", min_value=0.0, value=0.0, format="%.4f")
                        source = st.text_input("Quote source or provider")
                        fill_quote_time = st.text_input("Quote observed timestamp (ISO 8601 with timezone)",
                                                        placeholder="2026-11-15T15:30:00+05:30")
                        quantity = st.number_input("Fill quantity · 0 for all remaining", min_value=0, value=0, step=1)
                        submit_fill = st.form_submit_button("Simulate paper fill")
                    if submit_fill:
                        try:
                            quote = ExecutionQuote(last, source, datetime.fromisoformat(fill_quote_time.strip()),
                                                   bid=bid_value or None, ask=ask_value or None)
                            fill = engine.fill_order(choices[chosen_order], quote,
                                                     quantity=int(quantity) if quantity else None)
                            st.success(f"Paper fill recorded: {fill.quantity} share(s) at ₹{fill.fill_price:.4f}; estimated charge ₹{fill.transaction_cost:.2f}.")
                            st.rerun()
                        except (LookupError, ValueError, PaperExecutionError) as exc:
                            st.error(str(exc))

            positions = list(session.scalars(select(Positions).where(Positions.status == "open")))
            with st.expander("5 · Manual exits and stop/target monitoring"):
                if not positions:
                    st.info("There are no open paper positions.")
                else:
                    position_options = {f"{pos.stock.symbol} · {pos.quantity} shares · position #{pos.id}": pos.id for pos in positions}
                    with st.form("paper_manual_exit_form"):
                        selected_position = st.selectbox("Open position", list(position_options))
                        exit_price = st.number_input("Observed sell price · ₹", min_value=0.0, value=0.0, format="%.4f")
                        exit_source = st.text_input("Exit quote source")
                        exit_quote_time = st.text_input("Exit quote observed timestamp (ISO 8601 with timezone)",
                                                        placeholder="2026-11-15T15:30:00+05:30")
                        exit_qty = st.number_input("Exit quantity · 0 for full position", min_value=0, value=0, step=1)
                        submit_exit = st.form_submit_button("Approve and simulate manual exit")
                    if submit_exit:
                        try:
                            fill = engine.manual_exit(position_options[selected_position],
                                ExecutionQuote(exit_price, exit_source,
                                               datetime.fromisoformat(exit_quote_time.strip())),
                                quantity=int(exit_qty) if exit_qty else None)
                            st.success(f"Manual paper exit filled: {fill.quantity} share(s) at ₹{fill.fill_price:.4f}.")
                            st.rerun()
                        except (LookupError, ValueError, PaperExecutionError) as exc:
                            st.error(str(exc))
                    with st.form("paper_protection_form"):
                        protected = st.selectbox("Position to monitor", list(position_options))
                        bar_open = st.number_input("Completed bar open · ₹", min_value=0.0, value=0.0, format="%.4f")
                        bar_high = st.number_input("Completed bar high · ₹", min_value=0.0, value=0.0, format="%.4f")
                        bar_low = st.number_input("Completed bar low · ₹", min_value=0.0, value=0.0, format="%.4f")
                        bar_close = st.number_input("Completed bar close · ₹", min_value=0.0, value=0.0, format="%.4f")
                        bar_source = st.text_input("Completed bar source")
                        bar_time = st.text_input("Completed bar timestamp (ISO 8601 with timezone)",
                                                 placeholder="2026-11-15T15:30:00+05:30")
                        monitor = st.form_submit_button("Monitor stop-loss / target")
                    if monitor:
                        try:
                            selected_id = position_options[protected]
                            selected_position_record = session.get(Positions, selected_id)
                            fill = engine.monitor_stops(DailyMarketBar(selected_position_record.stock_id,
                                bar_open, bar_high, bar_low, bar_close, bar_source,
                                datetime.fromisoformat(bar_time.strip()), completed=True))
                            st.success(f"Protective paper exit filled at ₹{fill.fill_price:.4f}.") if fill else st.info("No stop-loss or target level was triggered.")
                            if fill:
                                st.rerun()
                        except (LookupError, ValueError, PaperExecutionError) as exc:
                            st.error(str(exc))

            st.markdown("### Persisted paper orders")
            if orders:
                st.dataframe(pd.DataFrame([{"Order": item.id, "Symbol": item.stock.symbol if item.stock else item.stock_id,
                    "Side": item.side, "Quantity": item.quantity, "Filled": item.filled_quantity,
                    "Average fill · ₹": float(item.average_price) if item.average_price is not None else None,
                    "Status": item.status, "Reason": item.reason} for item in orders]), hide_index=True, use_container_width=True)
            else:
                st.info("No paper orders have been created.")
            st.caption("Paper orders, fills, cash ledger, position levels, and status transitions are stored in SQLite. Live trading is disabled and no Upstox order API is used.")
    except Exception as exc:
        st.error(f"Paper trading database is unavailable: {type(exc).__name__}: {exc}")


def render_portfolio() -> None:
    service = TradingViewService()
    _intro("Portfolio", "Persistent local virtual capital and paper position marks.", service, "PAPER ACCOUNT")
    st.caption("Cash and realised P&L are reconstructed from the SQLite fill ledger. Enter sourced prices below to calculate unrealised P&L; no market price is fabricated here.")
    try:
        with SessionLocal() as session:
            engine = PaperTradingEngine(session)
            snapshot = engine.portfolio_snapshot()
            current_prices = {}
            if snapshot.positions:
                with st.form("portfolio_mark_prices"):
                    st.markdown("### Enter sourced current marks")
                    mark_source = st.text_input("Quote source/provider")
                    mark_time = st.text_input("Quote observed timestamp (ISO 8601 with timezone)",
                                              placeholder="2026-11-15T15:30:00+05:30")
                    for position in snapshot.positions:
                        current_prices[position.stock_id] = st.number_input(
                            f"{position.symbol} mark · ₹", min_value=0.0, value=0.0,
                            format="%.4f", key=f"portfolio-mark-{position.stock_id}")
                    update_marks = st.form_submit_button("Calculate marked P&L")
                if update_marks:
                    current_prices = {key: value for key, value in current_prices.items() if value > 0}
                    try:
                        observed_at = datetime.fromisoformat(mark_time.strip())
                        if observed_at.tzinfo is None:
                            raise ValueError("Timestamp must include a timezone.")
                        age = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
                        if not mark_source.strip() or not current_prices:
                            raise ValueError("Provide a quote source and a positive mark for each open position.")
                        if age < -60 or age > get_settings().paper_max_quote_age_seconds:
                            raise ValueError("Quote timestamp is stale or in the future; refresh current marks.")
                        st.session_state["paper_portfolio_marks"] = {
                            "prices": current_prices, "source": mark_source.strip(),
                            "observed_at": observed_at.isoformat()}
                        st.session_state["paper_portfolio_mark_error"] = None
                    except ValueError as exc:
                        st.session_state["paper_portfolio_mark_error"] = str(exc)
                else:
                    saved_marks = st.session_state.get("paper_portfolio_marks", {})
                    current_prices = saved_marks.get("prices", {})
                    if saved_marks:
                        saved_at = datetime.fromisoformat(saved_marks["observed_at"])
                        saved_age = (datetime.now(timezone.utc) - saved_at.astimezone(timezone.utc)).total_seconds()
                        if saved_age < -60 or saved_age > get_settings().paper_max_quote_age_seconds:
                            current_prices = {}
                            st.session_state.pop("paper_portfolio_marks", None)
                            st.warning("Previously entered marks expired. Unrealised P&L is hidden until you enter fresh quotes.")
                snapshot = engine.portfolio_snapshot(current_prices)
                if st.session_state.get("paper_portfolio_mark_error"):
                    st.error(st.session_state["paper_portfolio_mark_error"])
                mark_meta = st.session_state.get("paper_portfolio_marks")
                if mark_meta:
                    st.caption(f"Mark source: {mark_meta['source']} · observed {mark_meta['observed_at']}. User-supplied marks are short-lived session data, not persisted market quotes.")
            metric_cards([
                ("TOTAL VALUE", f"₹{snapshot.total_value:,.2f}" if snapshot.total_value is not None else "Not marked", "Cash + position value"),
                ("AVAILABLE CASH", f"₹{snapshot.available_cash:,.2f}", "After recorded charges"),
                ("INVESTED CAPITAL", f"₹{snapshot.invested_capital:,.2f}", f"{snapshot.open_positions} open position(s)"),
                ("REALISED P&L", f"₹{snapshot.realized_pnl:,.2f}", "After allocated entry and exit charges"),
                ("UNREALISED P&L", f"₹{snapshot.unrealized_pnl:,.2f}" if snapshot.unrealized_pnl is not None else "Not marked", "Uses entered price marks"),
            ], columns=5)
            if snapshot.positions:
                st.markdown("### Open paper positions")
                st.dataframe(pd.DataFrame([{"Symbol": p.symbol, "Quantity": p.quantity,
                    "Average purchase · ₹": float(p.average_entry_price),
                    "Mark · ₹": float(p.market_price) if p.market_price is not None else None,
                    "Invested · ₹": float(p.invested_capital),
                    "Unrealised P&L · ₹": float(p.unrealized_pnl) if p.unrealized_pnl is not None else None,
                    "Stop-loss · ₹": float(p.stop_loss) if p.stop_loss is not None else None,
                    "Target · ₹": float(p.target_price) if p.target_price is not None else None}
                    for p in snapshot.positions]), hide_index=True, use_container_width=True)
            else:
                st.info("No open paper positions.")
    except Exception as exc:
        st.error(f"Portfolio database is unavailable: {type(exc).__name__}: {exc}")


def render_journal() -> None:
    service = TradingViewService()
    _intro("Trade Journal", "Recorded paper fills and position lifecycle from the local ledger.", service, "PAPER LEDGER")
    with SessionLocal() as session:
        reports = ReportingService(session)
        closed = reports.paper_trades()
        open_positions = list(session.scalars(select(Positions).where(Positions.status == "open")
                                              .order_by(Positions.opened_at.desc())))
    rows = []
    if not closed.empty:
        for item in closed.to_dict("records"):
            rows.append({"ID": item["trade_id"], "Symbol": item["symbol"], "Status": "Closed" if item["position_id"] not in {p.id for p in open_positions} else "Partially open",
                         "Opened UTC": item["entry_at_utc"], "Closed UTC": item["exit_at_utc"],
                         "Quantity": item["quantity_exited"], "Entry · ₹": item["entry_price"],
                         "Exit · ₹": item["exit_price"], "Realised P&L · ₹": item["net_pnl_inr"],
                         "Charges · ₹": item["buy_and_sell_charges_inr"], "Exit reason": item["exit_reason"],
                         "Data source": item["market_data_source"]})
    for position in open_positions:
        if not closed.empty and position.id in set(closed["position_id"].tolist()):
            continue
        rows.append({"ID": f"POSITION-{position.id}", "Symbol": position.stock.symbol, "Status": "Open",
                     "Opened UTC": position.opened_at.isoformat(), "Closed UTC": None,
                     "Quantity": position.quantity, "Entry · ₹": float(position.average_entry_price),
                     "Exit · ₹": None, "Realised P&L · ₹": None, "Charges · ₹": None,
                     "Exit reason": "Open position", "Data source": "Paper fill ledger"})
    records = pd.DataFrame(rows)
    if records.empty:
        st.info("No paper positions or exit fills are recorded yet. The journal will populate from the local paper ledger.")
    else:
        col1, col2 = st.columns([1, 2])
        status = col1.selectbox("Trade status", ["All", "Open", "Partially open", "Closed"])
        search = col2.text_input("Search symbol or trade ID", key="journal_search")
        if status != "All":
            records = records[records["Status"] == status]
        if search.strip():
            records = records[records["Symbol"].str.contains(search.strip(), case=False, na=False) |
                              records["ID"].str.contains(search.strip(), case=False, na=False)]
        st.dataframe(records, hide_index=True, use_container_width=True,
                     column_config={"Entry · ₹": st.column_config.NumberColumn(format="₹%.2f"),
                                    "Exit · ₹": st.column_config.NumberColumn(format="₹%.2f"),
                                    "Realised P&L · ₹": st.column_config.NumberColumn(format="₹%.2f"),
                                    "Charges · ₹": st.column_config.NumberColumn(format="₹%.2f")})
        if records.empty:
            st.info("No ledger records match those filters.")
    st.markdown("### Add a local journal note")
    note = st.text_area("Journal note", placeholder="Write a review note for this browser session…")
    if st.button("Save note to this session", disabled=not note.strip()):
        st.session_state.setdefault("journal_notes", []).append(note.strip())
        st.success("Note saved in session state only; it is not a persisted trade record.")
    for item in st.session_state.get("journal_notes", []):
        st.markdown(f"- {item}")


def render_backtesting() -> None:
    service = TradingViewService()
    _intro("Historical Backtesting", "Event-driven evaluation of user-supplied completed NSE daily OHLCV data.", service, "HISTORICAL · SIMULATED EXECUTION")
    st.warning("Historical results are estimates, not guarantees of future returns. Confirm the data source and fee assumptions before interpreting results.")
    st.caption("No market data is invented or fetched by this page. It currently accepts user-uploaded CSV history only; source verification remains your responsibility.")
    upload = st.file_uploader("Historical daily OHLCV CSV", type=["csv"], help="Required columns: date or timestamp, open, high, low, close, volume. Keep the file's source and completed-through date documented.")
    settings = get_settings()
    left, middle, right = st.columns(3)
    symbol = left.text_input("NSE symbol", value="")
    capital = middle.number_input("Initial capital · ₹", min_value=1.0, value=float(settings.risk_starting_capital), step=500.0)
    slippage_bps = right.number_input("Slippage · basis points per side", min_value=0.0, max_value=1000.0, value=10.0, step=1.0)
    mode = st.selectbox("Evaluation", ["Chronological 70/30 holdout", "Walk-forward", "Full history"])
    source = st.text_input("Dataset source label", value="User-uploaded CSV (verify publisher/source)")
    if mode == "Walk-forward":
        c1, c2 = st.columns(2)
        train_bars = c1.number_input("Initial training bars", min_value=51, value=180, step=20)
        test_bars = c2.number_input("Bars per held-out fold", min_value=2, value=60, step=10)
    else:
        train_bars, test_bars = 180, 60
    file_digest = hashlib.sha256(upload.getvalue()).hexdigest() if upload is not None else None
    input_signature = (file_digest, symbol.strip().upper(), float(capital), float(slippage_bps), mode,
                       source.strip(), int(train_bars), int(test_bars))
    if st.button("Run historical backtest", type="primary", disabled=upload is None or not symbol.strip()):
        try:
            frame = pd.read_csv(upload)
            backtest_config = BacktestConfig(initial_capital=str(capital), slippage_bps=str(slippage_bps),
                                             risk_config=RiskConfig.from_settings(settings))
            engine = HistoricalBacktester(backtest_config=backtest_config)
            if mode == "Chronological 70/30 holdout":
                output = run_chronological_train_test(engine, frame, symbol=symbol, source=source)
                st.session_state["historical_backtest"] = ("holdout", output)
            elif mode == "Walk-forward":
                folds = walk_forward_test(engine, frame, symbol=symbol, initial_train_bars=int(train_bars),
                                          test_bars=int(test_bars), source=source)
                st.session_state["historical_backtest"] = ("walk_forward", folds)
            else:
                result = engine.run(frame, symbol=symbol, source=source)
                st.session_state["historical_backtest"] = ("full", result)
            st.session_state["historical_backtest_signature"] = input_signature
        except (InsufficientBacktestData, ValueError, TypeError, KeyError, pd.errors.ParserError) as exc:
            st.error(f"Backtest could not run: {exc}")
        except OSError as exc:
            st.error(f"Unable to read uploaded history: {exc}")

    stored = st.session_state.get("historical_backtest")
    if not stored:
        st.info("Upload a verified daily OHLCV CSV and run an evaluation. No results are shown until you provide historical data.")
        return
    if st.session_state.get("historical_backtest_signature") != input_signature:
        st.info("Inputs changed since the last run. Run the backtest again to refresh these results.")
        return
    result_type, output = stored
    if result_type == "holdout":
        train_result, result = output.train, output.test
        st.markdown(f"**Chronological split:** training through {output.split.train_end} · held-out testing {output.split.test_start} to {output.split.test_end}. Parameters remained fixed; training results were not used to optimize the test.")
        st.dataframe(pd.DataFrame([
            {"Period": "Train", "Net P&L · ₹": train_result.metrics.net_profit_loss,
             "Return · %": train_result.metrics.total_return_pct, "Trades": train_result.metrics.number_of_trades},
            {"Period": "Held-out test", "Net P&L · ₹": result.metrics.net_profit_loss,
             "Return · %": result.metrics.total_return_pct, "Trades": result.metrics.number_of_trades},
        ]), hide_index=True, use_container_width=True)
    elif result_type == "walk_forward":
        folds = output
        if not folds:
            st.warning("No complete walk-forward test folds were produced from this dataset.")
            return
        st.markdown("**Expanding-window walk-forward:** each fold tests a later window using prior history for indicator warm-up. Strategy parameters stayed fixed; no test-period optimization was performed.")
        st.dataframe(pd.DataFrame([{"Fold": f.fold, "Training through": f.training_end,
                                    "Test start": f.test_start, "Test end": f.test_end,
                                    "Net P&L · ₹": f.result.metrics.net_profit_loss,
                                    "Return · %": f.result.metrics.total_return_pct,
                                    "Trades": f.result.metrics.number_of_trades}
                                   for f in folds]), hide_index=True, use_container_width=True)
        result = folds[-1].result
        st.caption("Charts below show the final held-out fold. Use the fold table to compare periods.")
    else:
        result = output
    metrics = result.metrics
    metric_cards([
        ("FINAL CAPITAL", f"₹{metrics.final_capital:,.2f}", f"Initial ₹{metrics.initial_capital:,.2f}"),
        ("NET P&L", f"₹{metrics.net_profit_loss:,.2f}", f"Return {metrics.total_return_pct:.2f}%"),
        ("ANNUALISED RETURN", f"{metrics.annualized_return_pct:.2f}%" if metrics.annualized_return_pct is not None else "N/A", "Historical estimate"),
        ("MAX DRAWDOWN", f"{metrics.maximum_drawdown_pct:.2f}%", "Peak-to-trough"),
        ("WIN RATE", f"{metrics.win_rate_pct:.1f}%", f"{metrics.number_of_trades} trades"),
        ("PROFIT FACTOR", f"{metrics.profit_factor:.2f}" if metrics.profit_factor is not None else "N/A", "Gross wins / losses"),
        ("TOTAL COSTS", f"₹{metrics.total_transaction_costs:,.2f}", "Estimated charges + slippage"),
        ("AVG HOLD", f"{metrics.average_holding_period_sessions:.1f} sessions", f"Largest gain/loss ₹{metrics.largest_gain if metrics.largest_gain is not None else 0:.2f} / ₹{metrics.largest_loss if metrics.largest_loss is not None else 0:.2f}"),
    ], columns=4)
    for warning in result.warnings:
        st.warning(warning)
    st.caption(f"Source label: {result.source} · {result.symbol} · completed through {result.completed_through} · slippage {result.slippage_bps:g} bps per side · strategy {result.strategy_version}")
    equity_col, drawdown_col = st.columns(2)
    with equity_col:
        st.markdown("### Equity curve")
        fig = go.Figure(go.Scatter(x=result.equity_curve.index, y=result.equity_curve["equity"],
                                   mode="lines", line=dict(color="#A78BFA", width=2), name="Equity"))
        fig.update_layout(height=300, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color="#A1A1AA", yaxis_title="₹", xaxis_title="Test date")
        fig.update_yaxes(gridcolor="#303047")
        st.plotly_chart(fig, use_container_width=True)
    with drawdown_col:
        st.markdown("### Drawdown")
        fig = go.Figure(go.Scatter(x=result.equity_curve.index, y=result.equity_curve["drawdown_pct"],
                                   mode="lines", fill="tozeroy", line=dict(color="#EF4444"), name="Drawdown"))
        fig.update_layout(height=300, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                          font_color="#A1A1AA", yaxis_title="Drawdown · %", xaxis_title="Test date")
        fig.update_yaxes(gridcolor="#303047")
        st.plotly_chart(fig, use_container_width=True)
    month_col, distribution_col = st.columns(2)
    with month_col:
        st.markdown("### Monthly returns")
        if not result.monthly_returns.empty:
            fig = go.Figure(go.Bar(x=result.monthly_returns["month"], y=result.monthly_returns["return_pct"],
                                   marker_color=["#22C55E" if v >= 0 else "#EF4444" for v in result.monthly_returns["return_pct"]]))
            fig.update_layout(height=280, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              font_color="#A1A1AA", yaxis_title="Return · %")
            st.plotly_chart(fig, use_container_width=True)
    with distribution_col:
        st.markdown("### Trade distribution")
        if not result.trades.empty:
            fig = go.Figure(go.Histogram(x=result.trades["net_pnl"], nbinsx=16, marker_color="#8B5CF6"))
            fig.update_layout(height=280, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                              font_color="#A1A1AA", xaxis_title="Net trade P&L · ₹", yaxis_title="Trades")
            st.plotly_chart(fig, use_container_width=True)
            st.markdown("### Winning versus losing trades")
            outcome_counts = result.trades["net_pnl"].gt(0).value_counts()
            winners, losers = int(outcome_counts.get(True, 0)), int(outcome_counts.get(False, 0))
            pie = go.Figure(go.Pie(labels=["Winning", "Losing / breakeven"], values=[winners, losers], hole=.65,
                                    marker_colors=["#22C55E", "#EF4444"]))
            pie.update_layout(height=260, paper_bgcolor="rgba(0,0,0,0)", font_color="#F4F4F5")
            st.plotly_chart(pie, use_container_width=True)
        else:
            st.info("No completed trades occurred in this period; there is no trade distribution to display.")
    st.markdown("### Performance by entry market regime")
    st.dataframe(result.regime_metrics, hide_index=True, use_container_width=True)
    st.markdown("### Trade-by-trade records")
    st.dataframe(result.trades, hide_index=True, use_container_width=True)


def render_performance() -> None:
    page_header("Performance Analytics", "Ledger-backed paper results and isolated historical backtest reports.", right="ANALYTICS")
    st.info("Paper trading results and historical backtest results are calculated and exported separately. Live market data, when used as a quote source for a paper fill, is labeled as market data provenance; it is never reported as live trading P&L.")
    try:
        with SessionLocal() as session:
            reports = ReportingService(session)
            analytics = reports.paper_analytics()
            trades = reports.paper_trades()
            generated = analytics["generated_at"].isoformat()
            st.caption(f"Paper trading ledger · results: PAPER · generated {generated} · INR · no live-order results")
            metric_cards([
                ("DAILY P&L", format_inr(analytics["daily_pnl_inr"]), "Realized paper fills · IST day"),
                ("WEEKLY P&L", format_inr(analytics["weekly_pnl_inr"]), "Realized paper fills · IST week"),
                ("MONTHLY P&L", format_inr(analytics["monthly_pnl_inr"]), "Realized paper fills · IST month"),
                ("TOTAL RETURN", f"{analytics['total_return_pct']:.2f}%", "Realized paper P&L / initial capital"),
                ("INVESTED CAPITAL", format_inr(analytics["invested_capital_inr"]), "Open paper positions at average cost"),
                ("AVAILABLE CAPITAL", format_inr(analytics["available_capital_inr"]), "Paper ledger cash balance"),
                ("WIN RATE", f"{analytics['win_rate_pct']:.1f}%", f"{len(trades)} realized exit fills"),
                ("PROFIT FACTOR", f"{analytics['profit_factor']:.2f}" if analytics["profit_factor"] is not None and np.isfinite(analytics["profit_factor"]) else "N/A", "Gross net winners / gross net losers"),
                ("AVERAGE R MULTIPLE", f"{analytics['average_reward_risk']:.2f}R" if analytics["average_reward_risk"] is not None else "N/A", "Realized net P&L / configured stop risk"),
                ("MAX DRAWDOWN", f"{analytics['maximum_drawdown_pct']:.2f}%", "Realized paper equity sequence"),
                ("AVG HOLDING PERIOD", f"{analytics['average_holding_period_days']:.1f} days", "Closed exit fills"),
                ("TRADING COSTS", format_inr(analytics["trading_costs_inr"]), "Recorded charges + slippage"),
            ], columns=4)
            st.caption(analytics["unrealized_note"])
            provenance = analytics["paper_market_data_classes"] or ["No completed paper exits"]
            st.caption("Paper fill market-data provenance: " + ", ".join(provenance) +
                       ". Provenance does not change the PAPER result type.")
            st.markdown("### Strategy performance · paper results")
            strategy_frame = analytics["strategy_performance"]
            st.dataframe(strategy_frame, hide_index=True, use_container_width=True)
            st.markdown("### Paper performance grouped by market-data provenance")
            st.caption("A live quote may inform a paper fill, but the resulting P&L remains paper-trading P&L. These groups are kept separate by recorded quote source.")
            st.dataframe(analytics["paper_performance_by_market_data"], hide_index=True, use_container_width=True)
            if not trades.empty:
                st.markdown("### Realized paper P&L by exit date")
                plot = trades.sort_values("exit_at_utc").copy()
                plot["cumulative_pnl_inr"] = plot["net_pnl_inr"].cumsum()
                fig = go.Figure(go.Scatter(x=plot["trade_date_ist"], y=plot["cumulative_pnl_inr"], mode="lines+markers",
                                           line=dict(color="#A78BFA", width=2), name="Paper realized P&L"))
                fig.update_layout(height=300, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                  font_color="#A1A1AA", xaxis_title="Exit date · IST", yaxis_title="Cumulative P&L · ₹")
                fig.update_yaxes(gridcolor="#303047")
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(trades, hide_index=True, use_container_width=True)
            else:
                st.info("No completed paper exit fills are stored yet. Paper analytics will populate from the fill ledger; no sample P&L is substituted.")

            st.markdown("### Generate and download reports")
            report_kind = st.selectbox("Report", ["Daily trading report", "Weekly performance report",
                "Monthly performance report", "Individual trade report", "Strategy performance report",
                "Risk exposure report"], key="report_kind")
            report_date = st.date_input("Report date / period", value=datetime.now(ZoneInfo("Asia/Kolkata")).date(), key="report_date")
            selected_trade = None
            if report_kind == "Individual trade report" and not trades.empty:
                selected_trade = st.selectbox("Paper exit fill", trades["trade_id"].tolist(), key="report_trade_id")
            artifact = None
            try:
                if report_kind == "Daily trading report":
                    artifact = reports.daily_report(report_date)
                elif report_kind == "Weekly performance report":
                    artifact = reports.weekly_report(report_date)
                elif report_kind == "Monthly performance report":
                    artifact = reports.monthly_report(report_date)
                elif report_kind == "Individual trade report" and selected_trade:
                    artifact = reports.individual_trade_report(selected_trade)
                elif report_kind == "Strategy performance report":
                    artifact = reports.strategy_report()
                elif report_kind == "Risk exposure report":
                    artifact = reports.risk_exposure_report()
            except (LookupError, ValueError) as exc:
                st.warning(str(exc))
            if artifact is not None:
                st.caption(f"{artifact.name} · {artifact.result_type} · generated {artifact.generated_at.isoformat()} · {artifact.source_label}")
                stem = "".join(char.lower() if char.isalnum() else "_" for char in artifact.name).strip("_")
                csv_col, excel_col = st.columns(2)
                csv_col.download_button("Download CSV", reports.export_csv(artifact),
                    file_name=f"{stem}.csv", mime="text/csv", key="report_csv_download")
                excel_col.download_button("Download Excel", reports.export_excel(artifact),
                    file_name=f"{stem}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="report_excel_download")

            stored = st.session_state.get("historical_backtest")
            if stored:
                result_type, output = stored
                if result_type == "holdout":
                    backtest_result = output.test
                elif result_type == "walk_forward":
                    backtest_result = output[-1].result if output else None
                else:
                    backtest_result = output
                if backtest_result is not None:
                    st.markdown("### Latest historical backtest · separate dataset")
                    backtest_artifact = reports.backtest_report(backtest_result)
                    market_class = backtest_artifact.data["market_data_classification"].iloc[0] if not backtest_artifact.data.empty else "HISTORICAL_MARKET_DATA"
                    st.caption(f"HISTORICAL_BACKTEST_RESULT · {market_class} · source: {backtest_artifact.source_label} · generated {backtest_artifact.generated_at.isoformat()}")
                    st.dataframe(backtest_result.trades, hide_index=True, use_container_width=True)
                    backtest_stem = f"{backtest_result.symbol.lower()}_historical_backtest"
                    left, right = st.columns(2)
                    left.download_button("Download backtest CSV", reports.export_csv(backtest_artifact),
                        file_name=f"{backtest_stem}.csv", mime="text/csv", key="backtest_csv_download")
                    right.download_button("Download backtest Excel", reports.export_excel(backtest_artifact),
                        file_name=f"{backtest_stem}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="backtest_excel_download")
    except Exception as exc:
        st.error(f"Unable to build persisted paper analytics: {type(exc).__name__}")


def render_notifications() -> None:
    service = TradingViewService()
    settings = get_settings()
    _intro("Notifications", "Persisted alert delivery status and outbound-only Telegram controls.", service,
           "TELEGRAM ENABLED" if settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id else "TELEGRAM NOT CONFIGURED")
    st.info("Telegram is alert-only. Trade decisions can only be made in Trade Approval; incoming Telegram messages are not read or acted on.")
    with SessionLocal() as session:
        if st.button("Send daily paper trading report"):
            snapshot = PaperTradingEngine(session).portfolio_snapshot()
            message = (f"Daily paper report — capital ₹{snapshot.initial_capital:.2f}; "
                       f"cash ₹{snapshot.available_cash:.2f}; invested ₹{snapshot.invested_capital:.2f}; "
                       f"realised P&L ₹{snapshot.realized_pnl:.2f}; open positions {snapshot.open_positions}. "
                       "Paper results only; no live orders.")
            record = TelegramNotifier(session).daily_report(message)
            st.success(f"Daily report delivery status: {record.status}.")
        records = list(session.scalars(select(NotificationLogs).where(
            NotificationLogs.channel == "telegram").order_by(NotificationLogs.created_at.desc()).limit(100)))
        if records:
            st.dataframe([{"Created (UTC)": item.created_at, "Recipient": item.recipient,
                           "Message": item.message, "Status": item.status,
                           "Sent (UTC)": item.sent_at} for item in records],
                         hide_index=True, use_container_width=True)
        else:
            st.info("No Telegram notifications have been recorded.")


def render_settings() -> None:
    service = TradingViewService()
    _intro("Settings", "Effective local configuration and execution safety controls.", service, "LOCAL CONFIG")
    settings = get_settings()
    left, right = st.columns(2)
    with left:
        st.markdown("### Effective risk and execution configuration")
        st.dataframe(pd.DataFrame([
            {"Setting": "Execution mode", "Effective value": "Paper trading only" if settings.paper_trading_enabled else "Disabled"},
            {"Setting": "Live trading", "Effective value": "Permanently disabled"},
            {"Setting": "Starting virtual capital", "Effective value": format_inr(settings.risk_starting_capital)},
            {"Setting": "Risk per trade", "Effective value": f"{settings.risk_per_trade * 100:.2f}%"},
            {"Setting": "Maximum open positions", "Effective value": str(settings.risk_max_open_positions)},
            {"Setting": "Leverage / derivatives", "Effective value": "Disabled / Disabled"},
            {"Setting": "Minimum reward to risk", "Effective value": f"{settings.risk_min_reward_to_risk:.2f}×"},
            {"Setting": "Daily loss limit", "Effective value": f"{settings.risk_daily_loss_limit * 100:.2f}%"},
        ]), hide_index=True, use_container_width=True)
    with right:
        st.markdown("### Data and connectivity configuration")
        st.dataframe(pd.DataFrame([
            {"Setting": "Application environment", "Effective value": settings.app_env},
            {"Setting": "Database", "Effective value": "SQLite · local" if settings.database_url.startswith("sqlite") else "Configured external database"},
            {"Setting": "Upstox credentials", "Effective value": "Configured" if settings.upstox_client_id.get_secret_value() else "Not configured"},
            {"Setting": "Telegram alerts", "Effective value": "Configured" if settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id else "Not configured"},
            {"Setting": "AI explanations", "Effective value": settings.ai_provider if settings.ai_provider else "Disabled"},
            {"Setting": "Daily scheduler", "Effective value": "Enabled" if settings.daily_scheduler_enabled else "Disabled"},
        ]), hide_index=True, use_container_width=True)
    st.caption("Configuration is loaded from environment variables and .env. Edit those values locally and restart the relevant service to apply changes. Secret values are never displayed.")
    st.markdown("### Local repository status")
    snapshot = service.repository_snapshot()
    if snapshot.database_available:
        st.success("Local database repositories are reachable. Counts below include persisted application records only.")
        st.dataframe(pd.DataFrame([{"Repository": name, "Persisted records": count}
                                  for name, count in snapshot.record_counts.items()]),
                     hide_index=True, use_container_width=True)
    else:
        st.error(f"Database repositories are unavailable ({snapshot.error}).")
    st.caption("Credentials are not displayed or stored in this frontend. Configure secrets through environment variables.")
