"""
Async retry logic for transient ABBY API errors.

Policy taken straight from the ABBY "Error Handling" guide:

    HTTP 429            -> exponential backoff + jitter, up to 5 attempts
    HTTP 500 / 502 / 503-> retry with backoff + jitter, up to 3 attempts
    HTTP 400 - 404      -> do NOT retry (client error)
    HTTP 401 / 403      -> do NOT retry (auth/permission error)

If the API returns a `Retry-After` header we honour it, otherwise we fall
back to base_delay * 2**attempt plus full jitter (prevents thundering-herd
retries across many concurrent requests).
"""
import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, Optional

from app.core import config

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """Raised for an HTTP status the policy says we may retry."""

    def __init__(self, status_code: int, message: str,
                 retry_after: Optional[float] = None):
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"HTTP {status_code}: {message}")


class NonRetryableError(Exception):
    """Raised for an HTTP status / body error that must not be retried."""

    def __init__(self, status_code: int, message: str, body: Any = None):
        self.status_code = status_code
        self.message = message
        self.body = body
        super().__init__(f"HTTP {status_code}: {message}")


# ---------------------------------------------------------------------------
# Error-text classifiers (also used by the route layer to label results)
# ---------------------------------------------------------------------------
def is_token_limit_error(error_text: str) -> bool:
    """The request exceeded the model's context / token window. NOT retryable."""
    if not error_text:
        return False
    t = error_text.lower()
    phrases = (
        "maximum context length",
        "context_length_exceeded",
        "context length exceeded",
        "too many tokens",
        "token limit exceeded",
        "input too long",
        "prompt is too long",
        "prompt too long",
        "request too large",
        "maximum context",
        "context window",
    )
    return any(p in t for p in phrases)


def is_rate_limit_error(error_text: str) -> bool:
    if not error_text:
        return False
    t = error_text.lower()
    phrases = ("rate limit", "too many requests", "quota exceeded", "429")
    return any(p in t for p in phrases)


def is_file_too_large_error(error_text: str) -> bool:
    if not error_text:
        return False
    t = error_text.lower()
    return "maximum file size" in t or "file too large" in t or "413" in t


# ---------------------------------------------------------------------------
# Delay calculation
# ---------------------------------------------------------------------------
def _compute_delay(policy: dict, attempt: int,
                   retry_after: Optional[float]) -> float:
    if retry_after is not None and retry_after > 0:
        # The server told us exactly how long to wait.
        return min(retry_after, 60.0)

    base = policy["base_delay"]
    if policy["backoff"] == "exponential":
        backoff = base * (2 ** attempt)
    else:
        backoff = base
    # Full jitter: random(0, backoff). Spreads retries out evenly.
    return random.uniform(0, min(backoff, 60.0))


async def retry_async(call: Callable[..., Awaitable[Any]],
                      *args, **kwargs) -> Any:
    """
    Invoke an awaitable `call`, retrying per RETRY_POLICY.

    `call` is expected to raise:
        RetryableError    -> may be retried
        NonRetryableError -> propagated immediately
    Any other exception is treated as a 500-class error and retried.
    """
    last_exc: Optional[BaseException] = None

    for attempt in range(0, 6):  # at most 5 retries (policy max)
        try:
            return await call(*args, **kwargs)
        except NonRetryableError:
            raise
        except RetryableError as e:
            last_exc = e
            policy = config.RETRY_POLICY.get(e.status_code)
            if not policy or attempt >= policy["max_retries"]:
                logger.error(
                    "[HTTP %s] giving up after %d attempt(s): %s",
                    e.status_code, attempt + 1, e.message,
                )
                raise
            delay = _compute_delay(policy, attempt, e.retry_after)
            logger.warning(
                "[HTTP %s] %s — retry %d/%d in %.1fs",
                e.status_code, e.message, attempt + 1,
                policy["max_retries"], delay,
            )
            await asyncio.sleep(delay)
        except Exception as e:  # noqa: BLE001 - network/timeouts are retryable
            last_exc = e
            policy = config.RETRY_POLICY[500]
            if attempt >= policy["max_retries"]:
                logger.error("Unexpected error, giving up after %d attempts: %s",
                             attempt + 1, e)
                raise
            delay = _compute_delay(policy, attempt, None)
            logger.warning(
                "Transient error (%s); retry %d/%d in %.1fs",
                e, attempt + 1, policy["max_retries"], delay,
            )
            await asyncio.sleep(delay)

    # Should be unreachable, but be safe.
    if last_exc:
        raise last_exc
    raise RuntimeError("retry_async exited without a result")
