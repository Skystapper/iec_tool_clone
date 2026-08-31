"""
Application entry point for the IEC 62443 Compliance Analysis Tool.

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from app.core.logging_config import setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from app.core import config  # noqa: E402
from app.core.file_manager import UploadValidationError  # noqa: E402
from app.core.paths import PROJECT_ROOT, output_report_path  # noqa: E402
from app.core.rate_limiter import get_rate_limit_status  # noqa: E402
from app.routes.analyze import router  # noqa: E402

BASE_DIR = PROJECT_ROOT


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 70)
    logger.info("Starting IEC 62443 Compliance Analysis Tool")
    logger.info("ABBY base URL : %s", config.ABBY_BASE_URL)
    logger.info("Chat mode     : %s", config.ABBY_CHAT_MODE)
    if config.ABBY_CHAT_MODE == "agent":
        logger.info("Agent ID      : %s",
                    "set" if config.ABBY_AGENT_ID else "NOT SET")
    else:
        logger.info("Model         : %s", config.ABBY_MODEL)
    logger.info("API key       : %s",
                "set" if config.ABBY_API_KEY else "NOT SET")
    logger.info("Rate limit    : %d tokens/min (safety %.0f%%)",
                config.RATE_LIMIT_TOKENS_PER_MINUTE,
                config.RATE_LIMIT_SAFETY_FACTOR * 100)
    if not config.is_configured():
        logger.error("Configuration problem: %s",
                     config.missing_config_message())
    status = get_rate_limit_status()
    logger.info("Token bucket  : %d/%d available",
                status["available"], status["capacity"])
    logger.info("=" * 70)
    yield
    logger.info("Shutting down")


app = FastAPI(
    title="IEC 62443 Compliance Analysis Tool",
    description="Analyzes security documents against IEC 62443 via ABBY.",
    version="2.0.0",
    lifespan=lifespan,
)

app.include_router(router, prefix="/api")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Friendly error handlers
# ---------------------------------------------------------------------------
@app.exception_handler(UploadValidationError)
async def _upload_error_handler(_: Request, exc: UploadValidationError):
    return JSONResponse(status_code=400,
                        content={"success": False, "message": str(exc)})


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index2 - Currently using.html",
        context={
            "max_file_size_mb": config.MAX_FILE_SIZE_BYTES // (1024 * 1024),
            "allowed_extensions": config.SUPPORTED_EXTENSIONS_HUMAN,
        },
    )


@app.get("/health")
def health_check():
    return {
        "status": "healthy" if config.is_configured() else "degraded",
        "config": {
            "chat_mode": config.ABBY_CHAT_MODE,
            "model": config.ABBY_MODEL,
            "api_key_set": bool(config.ABBY_API_KEY),
        },
        "rate_limit": get_rate_limit_status(),
    }


@app.get("/test-config")
def test_config():
    return {
        "abby_configured": config.is_configured(),
        "base_url": config.ABBY_BASE_URL,
        "chat_mode": config.ABBY_CHAT_MODE,
        "model": config.ABBY_MODEL,
        "api_key_set": bool(config.ABBY_API_KEY),
        "agent_id_set": bool(config.ABBY_AGENT_ID),
        "missing": [] if config.is_configured()
        else config.missing_config_message(),
        "rate_limit": get_rate_limit_status(),
        "limits": {
            "max_file_size_mb": config.MAX_FILE_SIZE_BYTES // (1024 * 1024),
            "max_files_per_request": config.MAX_FILES_PER_REQUEST,
            "tokens_per_minute": config.RATE_LIMIT_TOKENS_PER_MINUTE,
            "output_token_reserve": config.OUTPUT_TOKEN_RESERVE,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
