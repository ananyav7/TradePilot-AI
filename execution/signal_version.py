"""Stable content fingerprint for a persisted strategy signal review."""

from __future__ import annotations

import hashlib

from database.models import TradeSignals


def signal_version(signal: TradeSignals) -> str:
    """Return a deterministic version binding the decision to its signal fields."""
    values = (signal.signal_key, signal.strategy_version, signal.reference_price,
              signal.stop_loss, signal.target_price,
              signal.quantity_placeholder, signal.rationale, signal.signal_type)
    digest = hashlib.sha256("|".join(str(value) for value in values).encode()).hexdigest()[:24]
    return f"{signal.strategy_version}:{digest}"
