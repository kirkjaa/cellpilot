"""Daily cap on live (paid) analyses, kept in the store as limits/<YYYY-MM-DD>.json (UTC days)."""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Optional

from .store import Store


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DailyLimit:
    def __init__(self, store: Store, cap: Optional[int] = None):
        self.store = store
        self.cap = cap if cap is not None else int(os.environ.get("CELLPILOT_DAILY_LIVE_RUNS", "10"))
        self._lock = threading.Lock()  # one instance (max-instances 1), so an in-process lock suffices

    def _key(self) -> str:
        return f"limits/{today()}.json"

    def used(self) -> int:
        return int((self.store.read_json(self._key()) or {}).get("count", 0))

    def left(self) -> int:
        return max(0, self.cap - self.used())

    def take(self) -> bool:
        """Count one live run; False when today's cap is already reached."""
        with self._lock:
            key = self._key()
            count = int((self.store.read_json(key) or {}).get("count", 0))
            if count >= self.cap:
                return False
            self.store.write_json(key, {"count": count + 1})
            return True
