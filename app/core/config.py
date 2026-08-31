"""
Central configuration for the IEC 62443 Compliance Analysis Tool.

All values are read from environment variables (optionally from a .env file).
This keeps secrets out of source code and lets the same build run against
different ABBY applications / models without code changes.

Reference: https://docs.abby.abb.com/
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# ABBY API credentials / endpoints
# ---------------------------------------------------------------------------
# Base URL for the ABBY Developer API (no trailing slash).
ABBY_BASE_URL = os.getenv("ABBY_BASE_URL", "https://api.abby.abb.com").rstrip("/")

# Authentication header: X-ABBY-API-Key
ABBY_API_KEY = os.getenv("ABBY_API_KEY")

# Which chat API to use:
#   "simple" -> POST /api/v1/developers/simple_chat (stateless, needs ABBY_MODEL)
#   "agent"  -> POST /api/v1/developers/agent_chat  (needs ABBY_AGENT_ID)
ABBY_CHAT_MODE = os.getenv("ABBY_CHAT_MODE", "simple").strip().lower()

# Model used by simple_chat. The docs recommend FAST models for summarising /
# processing large amounts of text (e.g. gpt-5-nano, gemini-3.5-flash,
# claude-4.5-haiku). Use abby_auto to let ABBY route to the best model.
ABBY_MODEL = os.getenv("ABBY_MODEL", "gpt-5-nano")

# Agent id used when ABBY_CHAT_MODE == "agent".
ABBY_AGENT_ID = os.getenv("ABBY_AGENT_ID")

# Sampling temperature (0.0 - 1.0 per docs).
ABBY_TEMPERATURE = float(os.getenv("ABBY_TEMPERATURE", "0.2"))

# Network timeouts (seconds). File uploads of large documents can take a while
# over slow or corporate-proxied connections. WRITE timeout is how long we wait
# between successful socket writes while pushing the file bytes up; READ timeout
# is how long we wait for the server's response after the request is sent.
ABBY_CONNECT_TIMEOUT = _get_int("ABBY_CONNECT_TIMEOUT", 30)
ABBY_READ_TIMEOUT = _get_int("ABBY_READ_TIMEOUT", 600)
ABBY_WRITE_TIMEOUT = _get_int("ABBY_WRITE_TIMEOUT", 600)

# ---------------------------------------------------------------------------
# Rate limiting  (docs: max 100,000 tokens / minute, rolling window)
# ---------------------------------------------------------------------------
RATE_LIMIT_TOKENS_PER_MINUTE = _get_int("RATE_LIMIT_TOKENS_PER_MINUTE", 100_000)

# Stay a little under the hard cap to absorb estimation error / other clients
# sharing the same application key.
RATE_LIMIT_SAFETY_FACTOR = float(os.getenv("RATE_LIMIT_SAFETY_FACTOR", "0.9"))

# Tokens reserved for the model's answer. Docs bill output too (chars / 4).
OUTPUT_TOKEN_RESERVE = _get_int("OUTPUT_TOKEN_RESERVE", 4_000)

# Seconds of in-flight time after which we still "release" reserved tokens so a
# crashed request can never permanently drain the bucket.
REQUEST_TIMEOUT_BUFFER = _get_int("REQUEST_TIMEOUT_BUFFER", 360)

# ---------------------------------------------------------------------------
# Upload limits  (docs: max 200 MB per file, max 100 files per application)
# ---------------------------------------------------------------------------
MAX_FILE_SIZE_BYTES = _get_int("MAX_FILE_SIZE_BYTES", 200 * 1024 * 1024)  # 200 MB
MAX_FILES_PER_REQUEST = _get_int("MAX_FILES_PER_REQUEST", 10)

# Formats accepted by the ABBY Files API upload endpoint.
# PDF / DOCX / PPTX go through the OCR/markdown pipeline (page quota applies);
# the textual/code/email formats are accepted as-is.
SUPPORTED_EXTENSIONS = {
    # OCR pipeline
    ".pdf", ".docx", ".pptx",
    # Web / text
    ".txt", ".md", ".html", ".htm", ".css", ".log",
    # Code
    ".py", ".go", ".cs", ".js", ".jsx", ".ts", ".tsx", ".c", ".java",
    ".cpp", ".hpp", ".h", ".rb", ".php", ".swift", ".kt", ".m", ".mm",
    ".r", ".sh", ".bash", ".yml", ".yaml",
    # Email
    ".msg", ".eml",
}

# Human-readable list shown in the UI / error messages.
SUPPORTED_EXTENSIONS_HUMAN = ".pdf, .docx, .pptx, .txt, .md, .html, .eml"

# Extensions that the ABBY upload endpoint explicitly REJECTS so we can fail
# fast with a clear message instead of an opaque 400 from the API.
REJECTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",  # images -> use input_image
    ".csv", ".xls", ".xlsx",                            # spreadsheets
    ".json",
}

# ---------------------------------------------------------------------------
# Files API behaviour
# ---------------------------------------------------------------------------
# Poll upload_status until it reaches "success" / "fail".
FILE_POLL_INTERVAL_SECONDS = float(os.getenv("FILE_POLL_INTERVAL_SECONDS", "3"))
FILE_POLL_MAX_WAIT_SECONDS = _get_int("FILE_POLL_MAX_WAIT_SECONDS", 600)

# Delete uploaded files from ABBY after analysis so the application never hits
# the 100-files-per-application cap.
DELETE_FILES_AFTER_ANALYSIS = _get_bool("DELETE_FILES_AFTER_ANALYSIS", True)

# ---------------------------------------------------------------------------
# Retry policy (docs: 429 -> exponential backoff, up to 5 attempts;
# 500/502/503 -> retry up to 3 times; 400-404 -> do not retry)
# ---------------------------------------------------------------------------
RETRY_POLICY = {
    429: {"max_retries": 5, "base_delay": 2.0, "backoff": "exponential"},
    500: {"max_retries": 3, "base_delay": 1.0, "backoff": "exponential"},
    502: {"max_retries": 3, "base_delay": 1.0, "backoff": "exponential"},
    503: {"max_retries": 3, "base_delay": 2.0, "backoff": "exponential"},
}


def is_configured() -> bool:
    """True iff enough config is present to call the ABBY API."""
    if not ABBY_API_KEY:
        return False
    if ABBY_CHAT_MODE == "agent":
        return bool(ABBY_AGENT_ID)
    return bool(ABBY_MODEL)


def missing_config_message() -> str:
    problems = []
    if not ABBY_API_KEY:
        problems.append("ABBY_API_KEY is not set")
    if ABBY_CHAT_MODE == "agent" and not ABBY_AGENT_ID:
        problems.append("ABBY_CHAT_MODE=agent but ABBY_AGENT_ID is not set")
    if ABBY_CHAT_MODE == "simple" and not ABBY_MODEL:
        problems.append("ABBY_CHAT_MODE=simple but ABBY_MODEL is not set")
    return "; ".join(problems) or "configuration looks OK"
