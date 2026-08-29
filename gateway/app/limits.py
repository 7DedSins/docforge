"""Per-caller rate limiting and concurrency caps.

Monthly quota answers "how much may you use". These answer "how fast", which is
a different question and the one that decides whether a single caller can make
the service unusable for everyone else.

Both are in-memory and per-process. That is deliberate: they protect *this*
box's capacity right now, and the gateway runs a single worker (see
ARCHITECTURE.md). State resetting on restart is acceptable — a restart already
frees the capacity these are protecting. Monthly quota, which must survive
restarts, lives in SQLite instead.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window request limiter, keyed by caller.

    A fixed window (e.g. "60 per calendar minute") lets a caller send 60 at
    59.9s and 60 more at 60.1s — 120 requests in 200ms, which is exactly the
    burst we are trying to prevent. A sliding window costs a deque per caller
    and does not have that hole.
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, caller: str, limit: int) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, seconds_until_retry)."""
        if limit <= 0:
            return True, 0

        now = time.monotonic()
        async with self._lock:
            q = self._hits[caller]
            cutoff = now - self.window
            while q and q[0] < cutoff:
                q.popleft()

            if len(q) >= limit:
                # Oldest hit leaving the window is when a slot frees up.
                return False, max(1, int(q[0] + self.window - now) + 1)

            q.append(now)
            # Callers who stop calling would otherwise leak a deque each.
            if not q:
                del self._hits[caller]
            return True, 0

    async def sweep(self) -> int:
        """Drop windows with no recent hits. Returns how many were removed."""
        cutoff = time.monotonic() - self.window
        async with self._lock:
            stale = [k for k, q in self._hits.items() if not q or q[-1] < cutoff]
            for k in stale:
                del self._hits[k]
        return len(stale)


class ConcurrencyGuard:
    """Caps how many requests one caller may have in flight at once.

    Without this, the global semaphores still protect the *host* — nothing
    crashes — but one caller firing 150 requests occupies every slot and
    everyone else queues behind them. Measured: 150 concurrent from one key
    pushed p50 latency to 15s for all other callers.

    This is a fairness control, not a safety one. The global semaphores remain
    the backstop.
    """

    def __init__(self) -> None:
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._inflight: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, caller: str, limit: int) -> bool:
        """Try to claim a slot. False means the caller is already at its cap."""
        if limit <= 0:
            return True
        async with self._lock:
            if self._inflight[caller] >= limit:
                return False
            self._inflight[caller] += 1
        return True

    async def release(self, caller: str, limit: int) -> None:
        if limit <= 0:
            return
        async with self._lock:
            self._inflight[caller] -= 1
            # Never let the map grow without bound across many one-off keys.
            if self._inflight[caller] <= 0:
                del self._inflight[caller]
                self._sems.pop(caller, None)

    def inflight(self, caller: str) -> int:
        return self._inflight.get(caller, 0)
