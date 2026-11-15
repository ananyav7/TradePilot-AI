"""Reusable visual components and shared terminal styling."""

from html import escape

import streamlit as st

from config.settings import get_settings

PALETTE = {
    "background": "#11111B", "surface": "#1B1B2B", "surface_raised": "#222237",
    "accent": "#8B5CF6", "accent_soft": "#A78BFA", "positive": "#22C55E",
    "negative": "#EF4444", "text": "#F4F4F5", "muted": "#A1A1AA", "border": "#303047",
}


def render_shell() -> None:
    """Apply the shared financial-terminal theme and execution/data status."""
    st.markdown(
        """
        <style>
        :root { color-scheme: dark; }
        .stApp { background: radial-gradient(ellipse at 50% -18%, rgba(139,92,246,.13), transparent 45%), #11111B; color:#F4F4F5; }
        [data-testid="stSidebar"] { background:#151522; border-right:1px solid #303047; }
        [data-testid="stHeader"] { background:rgba(17,17,27,.88); backdrop-filter:blur(12px); }
        [data-testid="stMetric"] { background:linear-gradient(145deg,#1d1d30,#191927); border:1px solid #34344c; padding:16px 18px; border-radius:14px; }
        [data-testid="stMetricLabel"] { color:#A1A1AA; }
        [data-testid="stMetricValue"] { color:#F4F4F5; font-size:1.55rem; }
        div[data-testid="stDataFrame"] { border:1px solid #303047; border-radius:12px; overflow:hidden; background:#171724; }
        .block-container { padding:1.7rem clamp(1rem,3vw,2.5rem) 3rem; max-width:1680px; }
        h1,h2,h3 { letter-spacing:-.035em; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
        h1 { font-size:clamp(1.75rem,2vw,2.25rem) !important; font-weight:750 !important; }
        h2 { font-size:1.3rem !important; font-weight:700 !important; }
        .tp-topline { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:0 0 24px; padding:12px 16px; background:rgba(27,27,43,.92); border:1px solid #34344c; border-radius:14px; }
        .tp-brand { color:#F4F4F5; font-weight:780; letter-spacing:.04em; }
        .tp-chip { display:inline-block; padding:5px 10px; border-radius:99px; background:rgba(139,92,246,.15); color:#C4B5FD; border:1px solid rgba(139,92,246,.35); font-size:.76rem; font-weight:650; }
        .tp-chip-warn { background:rgba(245,158,11,.1); border-color:rgba(245,158,11,.3); color:#FCD34D; }
        .tp-muted { color:#A1A1AA; font-size:.88rem; }
        .tp-card { background:linear-gradient(145deg,#1d1d30,#191927); border:1px solid #34344c; border-radius:14px; padding:16px 18px; min-height:112px; box-shadow:0 8px 24px rgba(0,0,0,.12); }
        .tp-card-label { color:#A1A1AA; font-size:.8rem; margin-bottom:10px; }
        .tp-card-value { color:#F4F4F5; font-size:1.45rem; font-weight:720; overflow-wrap:anywhere; }
        .tp-card-note { color:#A1A1AA; font-size:.78rem; margin-top:8px; }
        button[kind="primary"] { background:#8B5CF6; border-color:#8B5CF6; color:white; }
        button[kind="primary"]:hover { background:#7c3aed; border-color:#7c3aed; }
        [data-testid="stForm"] { background:#191927; border:1px solid #303047; border-radius:14px; padding:1rem; }
        [data-testid="stExpander"] { background:#171724; border:1px solid #303047; border-radius:12px; }
        [data-testid="stAlert"] { border-radius:12px; }
        @media (max-width:760px) {
          .block-container { padding:1rem .8rem 2rem; }
          .tp-topline { align-items:flex-start; flex-direction:column; }
          .tp-chip { margin:.12rem .08rem; }
        }
        </style>
        """, unsafe_allow_html=True,
    )
    settings = get_settings()
    st.markdown(
        '<div class="tp-topline"><div class="tp-brand">◈ &nbsp; TRADEPILOT <span class="tp-muted">/ AI TERMINAL</span></div>'
        '<div><span class="tp-chip">SOURCE-LABELED DATA</span> &nbsp; '
        '<span class="tp-chip">PAPER EXECUTION</span> &nbsp; <span class="tp-chip tp-chip-warn">LIVE ORDERS OFF</span></div></div>',
        unsafe_allow_html=True,
    )
    st.sidebar.caption(settings.app_name)
    st.sidebar.divider()
    st.sidebar.caption("Paper only · no broker orders")
    with st.sidebar.expander("Financial terms", expanded=False):
        st.caption("**Invested capital** · open positions valued at average fill cost.")
        st.caption("**Unrealised P&L** · mark-to-market change from a supplied price; charges may be excluded unless stated.")
        st.caption("**Realised P&L** · closed paper fills after recorded charges.")
        st.caption("**Risk/reward** · planned target distance divided by stop distance.")
        st.caption("**Relative volume** · latest volume divided by the prior 20-session average.")


def page_header(title: str, subtitle: str, *, right: str | None = None) -> None:
    """Render a consistent page title block."""
    col, status = st.columns([5, 2])
    with col:
        st.title(title)
        st.caption(subtitle)
    if right:
        with status:
            st.markdown(f'<div style="text-align:right;padding-top:20px"><span class="tp-chip">{escape(right)}</span></div>', unsafe_allow_html=True)


def metric_cards(items: list[tuple[str, str, str]], columns: int = 4) -> None:
    """Render compact KPI cards with escaped content and term tooltips."""
    for start in range(0, len(items), columns):
        batch = items[start:start + columns]
        cols = st.columns(len(batch))
        for col, (label, value, note) in zip(cols, batch):
            with col:
                st.markdown(
                    f'<div class="tp-card"><div class="tp-card-label" title="{escape(label)}">{escape(label)}</div>'
                    f'<div class="tp-card-value">{escape(value)}</div><div class="tp-card-note">{escape(note)}</div></div>',
                    unsafe_allow_html=True,
                )


def sample_notice(detail: str = "All figures on this page are illustrative sample values, not current NSE quotes.") -> None:
    """Keep simulated content clearly distinguished from live or persisted records."""
    st.info(f"**SIMULATED SAMPLE · NOT LIVE OR EXECUTABLE**  {detail}")


def render_source_badge(source: str, data_kind: str = "market data") -> None:
    """Show explicit provenance for data-backed page content."""
    st.caption(f"Data provenance · {escape(data_kind)}: {escape(source or 'source unavailable')}")
