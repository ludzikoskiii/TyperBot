"""Ogranicznik liczby zapytań na minutę (okno przesuwne)."""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from typerbot.data.errors import QuotaExceededError


class RateLimiter:
    def __init__(
        self,
        per_minute: int | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 90.0,
    ):
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self.max_wait = max_wait
        self._calls: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Czeka na wolne miejsce w oknie; wywołanie blokuje tylko wątek w tle."""
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._blocked_until - now)
            if self.per_minute:
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) >= self.per_minute:
                    wait = max(wait, 60.0 - (now - self._calls[0]))
            if wait > self.max_wait:
                raise QuotaExceededError(f"Limit zapytań na minutę – spróbuj za {wait:.0f} s")
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
            self._calls.append(now)

    def block_for(self, seconds: float) -> None:
        """Wstrzymuje zapytania (np. po odpowiedzi 429 lub wyczerpaniu minuty)."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, seconds))
