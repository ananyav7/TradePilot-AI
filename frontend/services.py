"""Read-only frontend access to persisted repository status."""

from dataclasses import dataclass

from database.connection import SessionLocal
from database.repositories import (
    ApprovalRepository,
    IndicatorsRepository,
    MarketDataRepository,
    NotificationLogRepository,
    PaperOrderRepository,
    PortfolioRepository,
    PositionRepository,
    StockRepository,
    SystemLogRepository,
    TradeHistoryRepository,
    TradeSignalRepository,
    WatchlistRepository,
)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Repository counts for persisted application records."""

    database_available: bool
    record_counts: dict[str, int]
    error: str | None = None


class TradingViewService:
    """Expose a read-only summary of repositories used by Streamlit pages."""

    @staticmethod
    def repository_snapshot() -> WorkspaceSnapshot:
        """Count stored records without creating or substituting demo rows."""
        repositories = {
            "Stocks": StockRepository,
            "Market data": MarketDataRepository,
            "Indicators": IndicatorsRepository,
            "Signals": TradeSignalRepository,
            "Approvals": ApprovalRepository,
            "Paper orders": PaperOrderRepository,
            "Positions": PositionRepository,
            "Trade history": TradeHistoryRepository,
            "Watchlists": WatchlistRepository,
            "Portfolios": PortfolioRepository,
            "System logs": SystemLogRepository,
            "Notifications": NotificationLogRepository,
        }
        try:
            with SessionLocal() as session:
                return WorkspaceSnapshot(True, {
                    label: len(repository(session).list(limit=500))
                    for label, repository in repositories.items()
                })
        except Exception as exc:
            return WorkspaceSnapshot(False, {}, error=type(exc).__name__)
