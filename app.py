"""TradePilot AI Streamlit application entry point."""

import logging
import os

import streamlit as st

# Community Cloud secrets must populate environment before modules construct the
# SQLAlchemy engine or cache application settings. Values remain server-side.
for _secret_name in ("APP_ENV", "APP_ROLE", "DATABASE_URL", "API_BASE_URL", "API_AUTH_TOKEN",
                     "APP_ACCESS_PASSWORD", "API_REQUEST_TIMEOUT_SECONDS", "SCHEDULER_MODE"):
    try:
        _secret_value = st.secrets.get(_secret_name)
    except Exception:
        _secret_value = None
    if _secret_value is not None and _secret_name not in os.environ:
        os.environ[_secret_name] = str(_secret_value)

from config.settings import get_settings  # noqa: E402
from frontend.components import render_shell  # noqa: E402
from frontend.api_client import api_health, require_frontend_access  # noqa: E402
from frontend.pages import (  # noqa: E402
    render_approvals, render_backtesting, render_dashboard, render_journal,
    render_market, render_notifications, render_paper_trading, render_performance,
    render_portfolio, render_scanner, render_settings, render_stock_analysis,
    render_signals,
)  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

st.set_page_config(
    page_title=f"{settings.app_name} · Terminal",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="auto",
)

pages = {
    "Overview": [st.Page(render_dashboard, title="Dashboard Overview", icon=":material/dashboard:", default=True)],
    "Markets": [
        st.Page(render_market, title="Market Overview", icon=":material/public:"),
        st.Page(render_scanner, title="NIFTY 50 Scanner", icon=":material/search:"),
        st.Page(render_stock_analysis, title="Stock Analysis", icon=":material/candlestick_chart:"),
    ],
    "Trading": [
        st.Page(render_signals, title="Trading Signals", icon=":material/ssid_chart:"),
        st.Page(render_approvals, title="Trade Approval", icon=":material/verified_user:"),
        st.Page(render_paper_trading, title="Paper Trading", icon=":material/receipt_long:"),
        st.Page(render_portfolio, title="Portfolio", icon=":material/account_balance_wallet:"),
        st.Page(render_journal, title="Trade Journal", icon=":material/menu_book:"),
    ],
    "Research": [
        st.Page(render_backtesting, title="Backtesting", icon=":material/science:"),
        st.Page(render_performance, title="Performance Analytics", icon=":material/analytics:"),
    ],
    "System": [
        st.Page(render_notifications, title="Notifications", icon=":material/notifications:"),
        st.Page(render_settings, title="Settings", icon=":material/settings:"),
    ],
}

if not require_frontend_access():
    st.stop()

page = st.navigation(pages, position="sidebar", expanded=True)
render_shell()
api_ok, api_message = api_health()
st.sidebar.caption(f"API · {api_message}" if api_ok else f"API · {api_message}")
page.run()
