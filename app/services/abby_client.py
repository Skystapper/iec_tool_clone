"""
Async client for the ABBY Simple Chat / Agent Chat endpoints.

Design points that fix the previous implementation:

  * Uses the Files API (file_id references) instead of inlining base64.
    Inline input_file is Gemini/PDF-only per the docs and forces the whole
    raw document into the prompt context; file_id lets ABBY inject the
    pre-extracted markdown and reuse it across requests.
  * Paces requests with the async token bucket BEFORE sending, reserving
    input + estimated output tokens (ABBY bills both at chars/4).
  * Reconciles the bucket with the real usage from the response when ABBY
    returns it, and honours Retry-After on 429s.
  * Supports both simple_chat and agent_chat via ABBY_CHAT_MODE.
"""
import logging
from typing import List, Optional

import httpx

from app.core import config
from app.core import rate_limiter
from app.core.rate_limiter import TokenRequestTooLarge
from app.core.retry_handler import (
    NonRetryableError,
    RetryableError,
    is_token_limit_error,
    retry_async,
)
from app.services import files_service, token_calculator

logger = logging.getLogger(__name__)

_SIMPLE_CHAT_PATH = "/api/v1/developers/simple_chat"
_AGENT_CHAT_PATH = "/api/v1/developers/agent_chat"
_USAGE_PATH = "/api/v1/developers/usage"


def _auth_headers() -> dict:
    return {"X-ABBY-API-Key": config.ABBY_API_KEY or ""}


def _extract_content(result: dict) -> Optional[str]:
    """Pull the assistant text out of a simple_chat/agent_chat response."""
    if not isinstance(result, dict):
        return None

    output = result.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(
                        block.get("text") or block.get("content") or ""
                    )
                else:
                    parts.append(str(block))
            return "\n".join(p for p in parts if p) or None
        return output.get("text")
    if isinstance(output, list):
        parts = []
        for block in output:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p) or None

    # Fallback for any other envelope.
    return result.get("response") or result.get("content")


def _extract_usage(result: dict):
    usage = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(usage, dict):
        return None, None
    inp = usage.get("input_tokens") or usage.get("prompt_tokens")
    out = usage.get("output_tokens") or usage.get("completion_tokens")
    return inp, out


def _classify_error(status: int, text: str) -> Exception:
    if status == 429:
        return RetryableError(429, f"Rate limit exceeded: {text[:300]}")
    if is_token_limit_error(text):
        return NonRetryableError(
            413,
            "The document is too large for the model's context window. "
            "Try a smaller/shorter document or a model with a larger "
            f"context window. ({text[:200]})",
            text,
        )
    if status in (500, 502, 503, 504):
        return RetryableError(status, f"ABBY server error: {text[:300]}")
    if status in (400, 401, 403, 404, 410):
        return NonRetryableError(status, text[:500], text)
    if status >= 500:
        return RetryableError(status, f"ABBY server error: {text[:300]}")
    return NonRetryableError(status, text[:500], text)


async def _send_once(client: httpx.AsyncClient, prompt: str,
                     file_ids: List[str]) -> dict:
    content = [{"type": "input_text", "text": prompt}]
    for fid in file_ids:
        content.append({
            "type": "input_file",
            "file": {"file_id": fid},
        })

    headers = {"Content-Type": "application/json", **_auth_headers()}

    if config.ABBY_CHAT_MODE == "agent":
        url = f"{config.ABBY_BASE_URL}{_AGENT_CHAT_PATH}"
        payload = {"agent_id": config.ABBY_AGENT_ID, "input": {
            "role": "user", "content": content,
        }}
    else:
        url = f"{config.ABBY_BASE_URL}{_SIMPLE_CHAT_PATH}"
        payload = {
            "model": config.ABBY_MODEL,
            "input": [{"role": "user", "content": content}],
            "temperature": config.ABBY_TEMPERATURE,
        }

    resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        text = ""
        try:
            body = resp.json()
            d = body.get("detail", body)
            if isinstance(d, dict):
                text = d.get("message") or d.get("details") or str(d)
            else:
                text = str(d)
        except Exception:  # noqa: BLE001
            text = resp.text
        raise _classify_error(resp.status_code, text or resp.text)

    return resp.json()


async def analyze(client: httpx.AsyncClient, prompt: str,
                  file_ids: List[str], estimated_tokens: int) -> dict:
    """
    Send one analysis request with rate-limit pacing and retries.

    Returns:
        {"success": bool, "content": str|None, "error": str|None,
         "error_kind": str|None}
    """
    # Reserve tokens up front (waits if the per-minute bucket is low). A
    # request larger than the bucket's capacity raises TokenRequestTooLarge so
    # the caller can report it instead of waiting forever.
    await rate_limiter.acquire_tokens(estimated_tokens)

    try:
        result = await retry_async(_send_once, client, prompt, file_ids)
    except TokenRequestTooLarge:
        # Do not refund / swallow -- this must propagate to the route so it can
        # produce a clear "document too large" result.
        raise
    except RetryableError as e:
        # Retries exhausted. Refund the reserve so other requests aren't
        # blocked by a call that produced no output tokens.
        await rate_limiter.refund_tokens(estimated_tokens)
        kind = "rate_limit" if e.status_code == 429 else "server"
        return {"success": False, "content": None,
                "error": str(e), "error_kind": kind}
    except NonRetryableError as e:
        await rate_limiter.refund_tokens(estimated_tokens)
        kind = "context_limit" if is_token_limit_error(e.message) else "client"
        return {"success": False, "content": None,
                "error": e.message, "error_kind": kind}
    except Exception as e:  # noqa: BLE001
        await rate_limiter.refund_tokens(estimated_tokens)
        return {"success": False, "content": None,
                "error": f"Unexpected error: {e}", "error_kind": "unknown"}

    # Reconcile the bucket with real usage when available.
    inp, out = _extract_usage(result)
    await rate_limiter.reconcile(estimated_tokens, inp, out)

    content = _extract_content(result)
    if not content:
        return {"success": False, "content": None,
                "error": "ABBY returned an empty response.",
                "error_kind": "empty"}
    return {"success": True, "content": content, "error": None,
            "error_kind": None}


async def get_usage(client: httpx.AsyncClient) -> Optional[dict]:
    """Fetch current-month usage from ABBY (best effort)."""
    try:
        resp = await client.get(
            f"{config.ABBY_BASE_URL}{_USAGE_PATH}", headers=_auth_headers()
        )
        if resp.status_code < 400:
            return resp.json()
        logger.warning("Usage API returned HTTP %s", resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not fetch usage: %s", e)
    return None
