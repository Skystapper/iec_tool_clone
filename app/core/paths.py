"""Filesystem paths anchored to the project root (parent of the `app` package)."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = PROJECT_ROOT / "uploads"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"

SDLC_XLSX = DATA_DIR / "SDLC.xlsx"

for _d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def output_report_path(session_id: str) -> Path:
    safe = str(session_id).replace("/", "_").replace("\\", "_")
    d = OUTPUT_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d / "report.xlsx"


def upload_dir(session_id: str) -> Path:
    safe = str(session_id).replace("/", "_").replace("\\", "_")
    d = UPLOAD_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d
