"""Time source for the engine, so timing logic can be tested without waiting."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from .api import CancelToken


class Clock:
    """Real time. Tests replace it with a fake that advances on ``sleep``."""

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float, cancel: Optional[CancelToken] = None) -> bool:
        """Sleep up to ``seconds``; True if ``cancel`` fired meanwhile."""
        if seconds <= 0:
            return bool(cancel and cancel.cancelled)
        if cancel is None:
            time.sleep(seconds)
            return False
        return cancel.wait(seconds)


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")
