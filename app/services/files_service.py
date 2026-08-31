"""
Async client for the ABBY Files API.

    POST   /api/v1/developers/files                    upload a file
    GET    /api/v1/developers/files?file_id=...        poll processing status
    GET    /api/v1/developers/files/{id}/content       raw or markdown content
    DELETE /api/v1/developers/files/{id}               delete a file

Docs: https://docs.abby.abb.com/developer/api/files/

Why we use this instead of inlining base64 in simple_chat:
  * Inline input_file only works for PDF on Gemini models.
  * The Files API supports pdf/docx/pptx/txt/md/html/code/emails.
  * A file is parsed ONCE and the markdown reused for every requirement,
    which is dramatically cheaper than re-sending base64 per request.
  * Max file size is 200 MB; max 100 files per application.
"""
import asyncio
import logging
import os
import random
from typing import Optional

import httpx

from app.core import config
from app.core.retry_handler import (
    NonRetryableError,
    RetryableError,
    is_file_too_large_error,
)

logger = logging.getLogger(__name__)

_FILES_PATH = "/api/v1/developers/files"

# Generous timeouts. Large PDFs on slow/corporate connections can take a long
# time to upload; the defaults in .env are 30s connect / 600s read / 600s write.
_TIMEOUT = httpx.Timeout(
    config.ABBY_CONNECT_TIMEOUT,
    read=config.ABBY_READ_TIMEOUT,
    write=config.ABBY_WRITE_TIMEOUT,
    pool=30.0,
)

# How many times to retry a transient network failure during upload, and the
# base backoff (seconds). These are independent of the HTTP-status retries in
# retry_handler — they cover connection resets, timeouts, DNS blips, etc.
UPLOAD_MAX_ATTEMPTS = 4
UPLOAD_BACKOFF_BASE = 3.0


def _auth_headers() -> dict:
    return {"X-ABBY-API-Key": config.ABBY_API_KEY or ""}


def _detail_from_response(resp: httpx.Response) -> str:
    """Extract ABBY's standardized error text from a response."""
    try:
        body = resp.json()
        d = body.get("detail", body)
        if isinstance(d, dict):
            return d.get("message") or d.get("details") or str(d)
        if isinstance(d, list):
            return "; ".join(
                (item.get("msg", str(item)) if isinstance(item, dict) else str(item))
                for item in d
            )
        return str(d)
    except Exception:  # noqa: BLE001
        return resp.text[:500]


def _raise_for_status(resp: httpx.Response) -> None:
    """Translate an HTTP error into RetryableError / NonRetryableError."""
    status = resp.status_code
    if status < 400:
        return

    detail = _detail_from_response(resp) or resp.text[:500] or f"HTTP {status}"

    if status == 413 or is_file_too_large_error(detail):
        raise NonRetryableError(
            413, f"File too large (ABBY limit is 200 MB): {detail}", detail
        )
    if status == 429:
        raise RetryableError(429, f"Rate limit exceeded: {detail}",
                             _parse_retry_after(resp))
    if status in (400, 401, 403, 404, 410):
        # 400 = unsupported type / page quota / referenced before processed;
        # 401/403 = auth/permission; 404/410 = missing/deleted. Not retryable.
        raise NonRetryableError(status, detail, detail)
    if status >= 500:
        raise RetryableError(status, f"ABBY server error: {detail}")

    raise NonRetryableError(status, detail, detail)


def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _classify_transport_error(exc: Exception, filename: str, size: int):
    """
    Turn an httpx/httpcore exception into either a RetryableError (network
    blip — safe to retry) or a NonRetryableError with a clear, actionable
    message. We always include the exception TYPE and repr because some
    low-level network errors otherwise stringify to an empty string.
    """
    name = type(exc).__name__
    detail = f"{name}: {exc!r}"

    if isinstance(exc, httpx.TimeoutException):
        if isinstance(exc, httpx.WriteTimeout):
            msg = (f"Upload of {filename} timed out while sending data "
                   f"({size/1_000_000:.1f} MB) after {config.ABBY_WRITE_TIMEOUT}s. "
                   "Your connection may be too slow or a proxy/firewall is "
                   "intercepting the upload. Increase ABBY_WRITE_TIMEOUT in "
                   ".env or try a smaller file / different network.")
        elif isinstance(exc, httpx.ReadTimeout):
            msg = (f"ABBY did not respond during upload of {filename} within "
                   f"{config.ABBY_READ_TIMEOUT}s. This can happen when ABBY is "
                   "busy; will retry.")
        else:
            msg = f"Upload of {filename} timed out ({detail}). Will retry."
        return RetryableError(504, msg)

    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError,
                        httpx.WriteError, httpx.ConnectError,
                        httpx.NetworkError)):
        msg = (f"Network error uploading {filename} ({size/1_000_000:.1f} MB): "
               f"{detail}. If you are behind a corporate proxy/VPN, try "
               "disabling it or configuring HTTPS_PROXY. Will retry.")
        return RetryableError(502, msg)

    # Anything else (including programming errors) — don't loop on it.
    return NonRetryableError(500, f"Unexpected error uploading {filename}: {detail}")


async def upload_file(client: httpx.AsyncClient, file_path: str) -> str:
    """
    Upload one file and return its file_id (does NOT wait for processing).

    Retries transient network failures / timeouts with exponential backoff +
    jitter. HTTP error responses are classified by _raise_for_status.
    """
    filename = os.path.basename(file_path)
    size = os.path.getsize(file_path)
    logger.info("Uploading %s to ABBY Files API (%.2f MB)", filename, size / 1e6)

    last_error: Optional[Exception] = None
    for attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
        try:
            with open(file_path, "rb") as handle:
                files = {"file": (filename, handle,
                                  "application/octet-stream")}
                resp = await client.post(
                    f"{config.ABBY_BASE_URL}{_FILES_PATH}",
                    headers=_auth_headers(),
                    files=files,
                )

            _raise_for_status(resp)

            try:
                data = resp.json()
            except ValueError as e:
                raise NonRetryableError(
                    502, f"ABBY returned a non-JSON response: {resp.text[:300]}"
                ) from e

            file_id = data.get("file_id")
            if not file_id:
                raise NonRetryableError(
                    502, f"No file_id in upload response: {data}"
                )
            logger.info("Uploaded %s -> file_id=%s (%s bytes)",
                        filename, file_id, data.get("bytes"))
            return file_id

        except NonRetryableError:
            raise
        except RetryableError as e:
            last_error = e
            logger.warning("Upload attempt %d/%d for %s failed: %s",
                           attempt, UPLOAD_MAX_ATTEMPTS, filename, e.message)
        except (httpx.HTTPError, OSError) as e:
            last_error = _classify_transport_error(e, filename, size)
            if isinstance(last_error, NonRetryableError):
                raise
            logger.warning("Upload attempt %d/%d for %s failed: %s",
                           attempt, UPLOAD_MAX_ATTEMPTS, filename,
                           last_error.message)

        if attempt < UPLOAD_MAX_ATTEMPTS:
            delay = UPLOAD_BACKOFF_BASE * (2 ** (attempt - 1))
            delay += random.uniform(0, 1.5)
            logger.info("Retrying upload of %s in %.1fs", filename, delay)
            await asyncio.sleep(delay)

    # All attempts exhausted.
    assert last_error is not None
    logger.error("Giving up on %s after %d attempts: %s",
                 filename, UPLOAD_MAX_ATTEMPTS, last_error)
    if isinstance(last_error, RetryableError):
        raise NonRetryableError(
            504,
            f"Could not upload {filename} after {UPLOAD_MAX_ATTEMPTS} attempts: "
            f"{last_error.message}",
        ) from last_error
    raise last_error


async def poll_until_ready(client: httpx.AsyncClient, file_id: str) -> dict:
    """
    Poll GET /files until upload_status == 'success' or 'fail'.

    Returns the file metadata dict on success.
    """
    deadline = asyncio.get_event_loop().time() + config.FILE_POLL_MAX_WAIT_SECONDS
    url = f"{config.ABBY_BASE_URL}{_FILES_PATH}"

    last_status = "pending"
    while True:
        resp = await client.get(
            url, headers=_auth_headers(), params={"file_id": file_id}
        )
        _raise_for_status(resp)
        items = resp.json()
        meta = items[0] if isinstance(items, list) and items else (items or {})
        status = meta.get("upload_status", "unknown")
        sub = meta.get("upload_sub_status", "")

        if status == "success":
            logger.info("file_id=%s ready (sub_status=%s)", file_id, sub)
            return meta
        if status == "fail":
            raise NonRetryableError(
                422, f"ABBY failed to process file {file_id}", meta
            )
        if status == "deleted":
            raise NonRetryableError(410, f"File {file_id} was deleted", meta)

        if status != last_status:
            logger.info("file_id=%s status=%s sub=%s", file_id, status, sub)
            last_status = status

        if asyncio.get_event_loop().time() > deadline:
            raise NonRetryableError(
                504,
                f"Timed out after {config.FILE_POLL_MAX_WAIT_SECONDS}s waiting "
                f"for file {file_id} (last status={status}/{sub})",
            )
        await asyncio.sleep(config.FILE_POLL_INTERVAL_SECONDS)


async def upload_and_wait(client: httpx.AsyncClient, file_path: str) -> str:
    """Convenience: upload then block until the markdown artifact is ready."""
    file_id = await upload_file(client, file_path)
    await poll_until_ready(client, file_id)
    return file_id


async def get_markdown(client: httpx.AsyncClient, file_id: str) -> str:
    """
    Fetch the markdown ABBY extracted for an uploaded file.

    This is the text that gets injected into the LLM context when we reference
    the file by file_id. Measuring its length lets us estimate token cost
    accurately (the raw PDF/DOCX byte size is NOT the right unit because those
    formats are compressed).
    """
    resp = await client.get(
        f"{config.ABBY_BASE_URL}{_FILES_PATH}/{file_id}/content",
        headers=_auth_headers(),
        params={"output": "markdown"},
    )
    _raise_for_status(resp)
    return resp.text or ""


async def delete_file(client: httpx.AsyncClient, file_id: str) -> None:
    """Best-effort delete. Never raises."""
    try:
        resp = await client.delete(
            f"{config.ABBY_BASE_URL}{_FILES_PATH}/{file_id}",
            headers=_auth_headers(),
        )
        if resp.status_code < 400:
            logger.info("Deleted ABBY file_id=%s", file_id)
        else:
            logger.warning("Could not delete file_id=%s: HTTP %s",
                           file_id, resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("Error deleting file_id=%s: %s", file_id, e)


def new_client() -> httpx.AsyncClient:
    """A shared httpx client configured for the ABBY Files API.

    trust_env=True (the default) means HTTPS_PROXY / HTTP_PROXY environment
    variables are honoured, which is usually what you want on corporate
    networks. Set them if your organisation routes traffic through a proxy.
    """
    return httpx.AsyncClient(timeout=_TIMEOUT, trust_env=True)
