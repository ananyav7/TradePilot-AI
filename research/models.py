"""Immutable evidence and report records for financial research explanations."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlparse


EvidenceKind = Literal["company_announcement", "market_news"]


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: EvidenceKind
    title: str
    summary: str
    url: str
    source: str
    published_at: datetime | None
    retrieved_at: datetime
    official_source: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Evidence links must be absolute HTTPS URLs")
        if not self.evidence_id or not self.title.strip() or not self.source.strip():
            raise ValueError("Evidence requires an ID, title, and source")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("Evidence publication timestamps must be timezone-aware")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("Evidence retrieval timestamps must be timezone-aware")
        if len(self.title) > 500 or len(self.summary) > 5000:
            raise ValueError("Evidence fields exceed the supported length")


@dataclass(frozen=True)
class AIInterpretation:
    summary: str
    risk_notes: tuple[str, ...]
    cited_evidence_ids: tuple[str, ...]
    provider: str
    model: str


@dataclass(frozen=True)
class SignalFacts:
    signal_id: str
    symbol: str
    signal_type: str
    signal_date: date
    signal_explanation: str
    entry_reference_price: float
    stop_loss: float | None
    target: float | None
    quantity_placeholder: int | None
    ema20: float
    ema50: float
    rsi14: float
    atr14: float
    relative_volume: float
    risk_reward_ratio: float | None
    ema_summary: str
    rsi_summary: str
    volume_summary: str
    risk_reward_summary: str
    interpretation_note: str


@dataclass(frozen=True)
class ResearchReport:
    facts: SignalFacts
    company_announcements: tuple[EvidenceItem, ...]
    market_news: tuple[EvidenceItem, ...]
    risks_and_uncertainties: tuple[str, ...]
    ai_interpretation: AIInterpretation | None
    ai_status: str
    ai_provider: str
    ai_model: str
    generated_at: datetime


@dataclass(frozen=True)
class DailyMarketSummary:
    market_date: date
    signal_counts: dict[str, int]
    signal_ids: tuple[str, ...]
    news: tuple[EvidenceItem, ...]
    factual_summary: str
    risks_and_uncertainties: tuple[str, ...]
    ai_interpretation: AIInterpretation | None
    ai_status: str
    generated_at: datetime
