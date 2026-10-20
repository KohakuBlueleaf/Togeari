"""A token bucket shared by the dispatcher (``capacity``, ``wake_at``) and the I/O loop."""

import asyncio
import threading
import time


class TokenBucket:
    """``rate`` tokens per second, holding at most ``burst``."""

    def __init__(self, rate: float, burst: float = 1.0):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = self.burst
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self.tokens = min(self.burst, self.tokens + (now - self.stamp) * self.rate)
        self.stamp = now

    def available(self) -> float:
        with self.lock:
            self._refill(time.monotonic())
            return self.tokens

    def due(self, n: float) -> float | None:
        """Monotonic time when ``n`` tokens will have been there to take; None when already."""
        with self.lock:
            now = time.monotonic()
            self._refill(now)
            if self.tokens >= n:
                return None
            return now + (n - self.tokens) / self.rate

    def take(self) -> float:
        """Take a token: 0.0 when taken, else the seconds until one is due."""
        with self.lock:
            self._refill(time.monotonic())
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            return (1.0 - self.tokens) / self.rate

    async def acquire(self) -> None:
        while (wait := self.take()) > 0.0:
            await asyncio.sleep(wait)
