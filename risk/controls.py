"""Persistent manual safety controls for the risk engine."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock


class KillSwitch:
    """Fail-closed local kill switch; deactivation always requires an explicit call."""

    def __init__(self, state_path: Path | str | None = None) -> None:
        if state_path is None:
            from config.settings import PROJECT_ROOT
            state_path = PROJECT_ROOT / "data" / "risk_kill_switch.json"
        self.state_path = Path(state_path)
        self._lock = RLock()

    @property
    def is_active(self) -> bool:
        with self._lock:
            if not self.state_path.exists():
                return False
            try:
                document = json.loads(self.state_path.read_text(encoding="utf-8"))
                return document.get("active") is True
            except (OSError, ValueError, TypeError):
                # Corrupt/unreadable control state must stop new risk.
                return True

    def activate(self, reason: str) -> None:
        reason = reason.strip()
        if not reason:
            raise ValueError("A reason is required to activate the kill switch")
        self._write({"active": True, "reason": reason[:500],
                     "changed_at": datetime.now(timezone.utc).isoformat()})

    def deactivate(self, reason: str) -> None:
        reason = reason.strip()
        if not reason:
            raise ValueError("A reason is required to deactivate the kill switch")
        self._write({"active": False, "reason": reason[:500],
                     "changed_at": datetime.now(timezone.utc).isoformat()})

    def _write(self, document: dict) -> None:
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.state_path)
