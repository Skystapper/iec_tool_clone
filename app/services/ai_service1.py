"""
Backwards-compatible wrapper around the new async ABBY client.

The original project called `call_ai(prompt, file_paths)` synchronously and
expected a dict with an `output.content` key. The implementation now uses the
ABBY Files API + async chat client (see ``abby_client`` and ``files_service``).

This module keeps the old name so any external/legacy imports keep working,
but new code should call ``app.services.abby_client.analyze`` directly.
"""
import asyncio
import logging
import os
from typing import List

from app.core import config
from app.services import files_service
from app.services.abby_client import analyze as _analyze
from app.services.token_calculator import estimate_request_tokens

logger = logging.getLogger(__name__)


def _resolve_config():
    """Re-read env so tests that set env after import still work."""
    return {
        "api_key": os.getenv("ABBY_API_KEY"),
        "agent_id": os.getenv("ABBY_AGENT_ID"),
        "api_url": os.getenv("ABBY_API_URL"),
    }


async def _call_ai_async(prompt: str, file_paths: List[str]):
    if not config.is_configured():
        logger.error("ABBY not configured: %s", config.missing_config_message())
        return None

    async with files_service.new_client() as client:
        file_ids = []
        try:
            for fp in file_paths:
                file_ids.append(await files_service.upload_and_wait(client, fp))
            est = estimate_request_tokens(
                prompt, file_paths, config.OUTPUT_TOKEN_RESERVE
            )
            outcome = await _analyze(client, prompt, file_ids, est)
        finally:
            if config.DELETE_FILES_AFTER_ANALYSIS:
                for fid in file_ids:
                    await files_service.delete_file(client, fid)

    if not outcome["success"]:
        logger.warning("ABBY analysis failed: %s", outcome.get("error"))
        return None

    # Return the legacy envelope shape.
    return {"output": {"content": outcome["content"], "type": "text"}}


def call_ai(prompt: str, file_paths: List[str]):
    """
    Synchronous wrapper retained for backwards compatibility.

    Inside the async FastAPI app, prefer ``abby_client.analyze`` directly.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context; spin up a sub-loop in a thread so
            # we don't block the event loop.
            import threading
            box = {}

            def _runner():
                box["result"] = asyncio.run(_call_ai_async(prompt, file_paths))

            t = threading.Thread(target=_runner)
            t.start()
            t.join()
            return box.get("result")
        return loop.run_until_complete(_call_ai_async(prompt, file_paths))
    except RuntimeError:
        return asyncio.run(_call_ai_async(prompt, file_paths))
