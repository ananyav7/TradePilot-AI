"""Deterministic signal explanations and sourced market research summaries."""

from __future__ import annotations

from datetime import date, datetime, timezone

from research.ai import MarketResearchAI
from research.models import DailyMarketSummary, EvidenceItem, ResearchReport, SignalFacts
from research.sources import EmptyResearchContentProvider, ResearchContentProvider
from strategy.configuration import StrategyConfig, load_strategy_config
from strategy.swing_strategy import StrategySignal


class MarketResearchService:
    def __init__(self, content: ResearchContentProvider | None = None,
                 ai: MarketResearchAI | None = None, strategy_config: StrategyConfig | None = None) -> None:
        self.content = content or EmptyResearchContentProvider()
        self.ai = ai or MarketResearchAI.from_settings()
        self.strategy_config = strategy_config or load_strategy_config()

    def explain_signal(self, signal: StrategySignal, company_name: str | None = None) -> ResearchReport:
        facts = self._facts(signal)
        announcements = tuple(self.content.company_announcements(signal.symbol, company_name or signal.symbol))
        news = tuple(self.content.market_news(signal.symbol, company_name or signal.symbol))
        evidence = announcements + news
        risks = ["Research is informational and does not authorize or execute a trade.",
                 "Indicator values use the completed candle snapshot and may not reflect later market conditions.",
                 "Reference prices and risk/reward omit slippage, taxes, liquidity, and fill uncertainty."]
        if signal.signal_type == "NO TRADE":
            risks.append("No trade was generated; stop, target, and risk/reward are hypothetical scenario levels only.")
        errors = getattr(self.content, "source_errors", [])
        if errors:
            risks.append("One or more configured research sources could not be retrieved; coverage is incomplete.")
        if not announcements:
            risks.append("No matching company announcements were retrieved from configured sources; this does not establish that none exist.")
        if not news:
            risks.append("No matching market news was retrieved from configured sources; this does not establish that none exists.")
        ai_result = self.ai.interpret(self._ai_facts(facts), evidence)
        status = "enabled" if ai_result else ("disabled" if not self.ai.enabled else "unavailable_or_rejected")
        return ResearchReport(facts, announcements, news, tuple(risks), ai_result, status,
                             ai_result.provider if ai_result else "disabled",
                             ai_result.model if ai_result else "", datetime.now(timezone.utc))

    def daily_summary(self, market_date: date, signals: list[StrategySignal],
                      news: tuple[EvidenceItem, ...] | None = None) -> DailyMarketSummary:
        todays = [s for s in signals if s.signal_date == market_date]
        counts = {kind: sum(s.signal_type == kind for s in todays) for kind in ("BUY", "HOLD", "NO TRADE")}
        retrieved = news if news is not None else tuple(self.content.market_news("", "NIFTY NSE India"))
        lines = [f"{len(todays)} stored strategy signal(s) dated {market_date.isoformat()}: " +
                 ", ".join(f"{key} {value}" for key, value in counts.items()) + "."]
        if retrieved:
            lines.append(f"Retrieved {len(retrieved)} market news item(s); see linked source records for timestamps and details.")
        else:
            lines.append("No matching market news was retrieved from configured sources; this does not establish that none exists.")
        risks = ["Summary covers only supplied stored signals and retrieved sources; it does not describe unobserved market activity.",
                 "This research summary does not authorize or execute trades."]
        ai_result = self.ai.interpret({"market_date": market_date.isoformat(), "signal_counts": counts,
                                       "signal_ids": [s.signal_id for s in todays], "factual_summary": " ".join(lines)}, retrieved)
        status = "enabled" if ai_result else ("disabled" if not self.ai.enabled else "unavailable_or_rejected")
        return DailyMarketSummary(market_date, counts, tuple(s.signal_id for s in todays), tuple(retrieved),
                                  " ".join(lines), tuple(risks), ai_result, status, datetime.now(timezone.utc))

    def _facts(self, signal: StrategySignal) -> SignalFacts:
        cfg = self.strategy_config.entry
        ema = (f"EMA20 {signal.ema20:.4f} is {'above' if signal.ema20 > signal.ema50 else 'at or below'} "
               f"EMA50 {signal.ema50:.4f}; reference close {signal.entry_reference_price:.4f} is "
               f"{'above' if signal.entry_reference_price > signal.ema20 else 'at or below'} EMA20.")
        rsi_pass = cfg.rsi_min <= signal.rsi14 <= cfg.rsi_max
        rsi = f"RSI14 is {signal.rsi14:.2f}; configured entry interval is {cfg.rsi_min:g}–{cfg.rsi_max:g} ({'within' if rsi_pass else 'outside'})."
        volume_pass = signal.volume_ratio > cfg.relative_volume_min_exclusive
        volume = f"Relative volume is {signal.volume_ratio:.2f}x; configured requirement is > {cfg.relative_volume_min_exclusive:g}x ({'passed' if volume_pass else 'not passed'})."
        if signal.stop_loss is not None and signal.target is not None and signal.risk_reward_ratio is not None:
            risk = signal.entry_reference_price - signal.stop_loss
            reward = signal.target - signal.entry_reference_price
            rr = (f"Per-share reference risk = entry {signal.entry_reference_price:.4f} − stop {signal.stop_loss:.4f} = {risk:.4f}; "
                  f"reference reward = target {signal.target:.4f} − entry = {reward:.4f}; ratio = reward / risk = {signal.risk_reward_ratio:.4f}:1.")
        else:
            rr = "Risk/reward is unavailable because valid stop and target reference levels are not present."
        if signal.signal_type == "NO TRADE" and signal.stop_loss is not None and signal.target is not None:
            rr = "Hypothetical levels only (no active trade plan): " + rr
        if signal.signal_type == "NO TRADE":
            note = f"No trade generated: {signal.strategy_explanation}"
        elif signal.signal_type == "BUY":
            note = "Scanner conditions were recorded as passed for this completed candle. " + signal.strategy_explanation
        else:
            note = signal.strategy_explanation
        return SignalFacts(signal.signal_id, signal.symbol, signal.signal_type, signal.signal_date, note,
                           signal.entry_reference_price, signal.stop_loss, signal.target,
                           signal.quantity_placeholder, signal.ema20, signal.ema50, signal.rsi14,
                           signal.atr14, signal.volume_ratio, signal.risk_reward_ratio,
                           ema, rsi, volume, rr, "These are recorded strategy facts; they do not change the signal or trading parameters.")

    @staticmethod
    def _ai_facts(facts: SignalFacts) -> dict:
        return {"signal_id": facts.signal_id, "symbol": facts.symbol, "signal_type": facts.signal_type,
                "signal_date": facts.signal_date.isoformat(), "strategy_explanation": facts.signal_explanation,
                "ema_summary": facts.ema_summary, "rsi_summary": facts.rsi_summary,
                "volume_summary": facts.volume_summary, "risk_reward_summary": facts.risk_reward_summary,
                "interpretation_note": facts.interpretation_note}
