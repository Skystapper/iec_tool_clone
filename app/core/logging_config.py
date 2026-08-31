import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Anchor logs/ to the project root (the parent of the `app` package) so logs
# always land in the same place regardless of the current working directory.
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def setup_logging():
    """
    Configure logging to write to both console and file.
    
    Files created:
    - logs/app.log          → All logs
    - logs/errors.log       → Only errors & critical
    - logs/retry_debug.log  → Retry logic details
    """
    
    # ===== ROOT LOGGER =====
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # ===== LOG FORMAT =====
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # ===== CONSOLE HANDLER (print to terminal) =====
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # ===== FILE HANDLER: ALL LOGS =====
    # Rotates when file reaches 5MB, keeps 5 backup files
    file_handler = RotatingFileHandler(
        str(LOG_DIR / "app.log"),
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # ===== FILE HANDLER: ERRORS ONLY =====
    error_handler = RotatingFileHandler(
        str(LOG_DIR / "errors.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)
    
    # ===== FILE HANDLER: RETRY LOGIC DEBUG =====
    retry_handler = RotatingFileHandler(
        str(LOG_DIR / "retry_debug.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=5
    )
    retry_handler.setLevel(logging.DEBUG)
    retry_handler.setFormatter(formatter)
    
    # Attach to retry_handler logger specifically
    retry_logger = logging.getLogger("app.core.retry_handler")
    retry_logger.addHandler(retry_handler)
    
    print(f"✅ Logging configured. Logs stored in: {LOG_DIR}/")
    
    return root_logger
