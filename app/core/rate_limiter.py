"""
Async token-bucket rate limiter for the ABBY per-minute token quota.

ABBY enforces:
    * 100,000 tokens per minute, rolling window  (HTTP 429 when exceeded)
    * a monthly allocation                       (HTTP 429 when exhausted)

This module is the FIRST line of defence: it paces outgoing requests so we
stay under the per-minute cap. It is async (uses asyncio.sleep) so it never
blocks the FastAPI event loop, keeps a configurable safety margin below the
hard cap, and reserves tokens for the model's output as well as its input.

The ABBY client (app/services/abby_client.py) is the SECOND line: it reads
the real token usage from the API response and calls report_actual_usage(),
and it honours Retry-After on any 429 that still slips through.
"""
import asyncio
import time
import logging
from typing import Optional

from app.core import config

logger = logging.getLogger(__name__)


class TokenRequestTooLarge(Exception):
    """
    Raised when a single request asks for more tokens than the bucket can ever
    hold (i.e. it exceeds the per-minute limit even with a completely empty
    minute). Such a request can never succeed and must not be awaited forever.
    """

    def __init__(self, requested: int, capacity: int):
        self.requested = requested
        self.capacity = capacity
        super().__init__(
            f"Request needs {requested:,} tokens but the per-minute capacity "
            f"is only {capacity:,}. The document is too large to send in a "
            "single request; split it or use a shorter document."
        )


class AsyncTokenBucket:
    """
    Leaky-bucket style limiter refilled continuously at `rate` tokens/sec.

    Capacity is the per-minute limit * safety factor, so even with estimation
    error and other clients sharing the same application key we stay under the
    hard 100k/min ceiling.
    """

    def __init__(self, tokens_per_minute: int, safety_factor: float = 0.9):
        self.capacity = int(tokens_per_minute * safety_factor)
        # Refill the full capacity over a 60-second window.
        self.rate = self.capacity / 60.0
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    async def acquire(self, tokens: int) -> int:
        """
        Wait until `tokens` are available, then consume them.

        Returns the number of tokens actually consumed.
        """
        if tokens > self.capacity:
            # Even a fully-refreshed minute cannot serve this request. Refusing
            # fast is much better than looping forever (which previously made
            # the UI hang with "waiting 2432.4s").
            raise TokenRequestTooLarge(tokens, self.capacity)

        await self._lock.acquire()
        try:
            self._refill_locked()
            while self._tokens < tokens:
                deficit = tokens - self._tokens
                # Wait long enough to accrue the deficit, then re-check.
                wait = deficit / self.rate
                logger.warning(
                    "Rate limiter: need %.0f tokens, have %.0f/%.0f; "
                    "waiting %.1fs",
                    tokens, self._tokens, self.capacity, wait,
                )
                # Release the lock while sleeping so other tasks can proceed,
                # then re-acquire before touching shared state again.
                self._lock.release()
                try:
                    await asyncio.sleep(min(wait, 5.0))
                finally:
                    await self._lock.acquire()
                self._refill_locked()
            self._tokens -= tokens
            return tokens
        finally:
            # Only release if we still hold it (the sleep branch re-acquires).
            if self._lock.locked():
                self._lock.release()

    async def refund(self, tokens: int) -> None:
        """Return unused tokens to the bucket (e.g. request failed early)."""
        async with self._lock:
            self._refill_locked()
            self._tokens = min(self.capacity, self._tokens + tokens)

    def available(self) -> float:
        # Approximate; no lock needed for a status readout.
        elapsed = time.monotonic() - self._last
        return min(self.capacity, self._tokens + elapsed * self.rate)

    def status(self) -> dict:
        avail = self.available()
        return {
            "available": int(avail),
            "capacity": self.capacity,
            "hard_limit_per_minute": config.RATE_LIMIT_TOKENS_PER_MINUTE,
            "used_percent": round((1 - avail / self.capacity) * 100, 1),
            "refill_tokens_per_second": round(self.rate, 1),
        }


# Process-wide bucket (matches the per-application ABBY quota).
bucket = AsyncTokenBucket(
    tokens_per_minute=config.RATE_LIMIT_TOKENS_PER_MINUTE,
    safety_factor=config.RATE_LIMIT_SAFETY_FACTOR,
)


async def acquire_tokens(estimated: int) -> int:
    """Reserve `estimated` tokens before a request. Returns the amount reserved."""
    return await bucket.acquire(estimated)


async def refund_tokens(tokens: int) -> None:
    if tokens and tokens > 0:
        await bucket.refund(tokens)


async def reconcile(estimated: int, actual_input: Optional[int],
                    actual_output: Optional[int]) -> None:
    """
    Correct the bucket after we know the API's real token usage.

    The ABBY simple_chat response does not always include a usage block, so
    `actual_*` may be None. When present, we refund the over-estimate or charge
    the under-estimate so the bucket tracks reality over time.
    """
    if actual_input is None and actual_output is None:
        return
    actual_total = (actual_input or 0) + (actual_output or 0)
    diff = estimated - actual_total
    if diff > 0:
        await bucket.refund(diff)
        logger.info("Token reconcile: refunded %d over-estimate", diff)
    elif diff < 0:
        # We under-estimated; drain the extra (may wait if bucket is low).
        extra = -diff
        logger.warning("Token reconcile: under-estimated by %d; draining", extra)
        await bucket.acquire(extra)


def get_rate_limit_status() -> dict:
    return bucket.status()


def reset_rate_limiter() -> None:
    """Test helper: reset the bucket to full capacity."""
    global bucket
    bucket = AsyncTokenBucket(
        tokens_per_minute=config.RATE_LIMIT_TOKENS_PER_MINUTE,
        safety_factor=config.RATE_LIMIT_SAFETY_FACTOR,
    )
