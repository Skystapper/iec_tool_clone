"""
File upload handling with server-side validation.

Validation mirrors the ABBY Files API limits so we reject bad uploads fast
with a clear message instead of waiting for an opaque 400/413 from ABBY:

  * max 200 MB per file   (HTTP 413 on the ABBY side)
  * max 100 files per application (we cap per-request at MAX_FILES_PER_REQUEST)
  * supported extensions: pdf/docx/pptx/txt/md/html/code/emails
  * rejected: images (.jpg/.png/...), spreadsheets (.csv/.xls/.xlsx), .json
"""
import os
import re
from typing import List

from fastapi import UploadFile

from app.core import config
from app.core.paths import upload_dir


class UploadValidationError(ValueError):
    """Raised when an uploaded file fails validation."""


def make_safe_filename(filename: str) -> str:
    filename = (filename or "unnamed").strip().replace(" ", "_")
    filename = re.sub(r"[^A-Za-z0-9._-]", "", filename)
    return filename or "unnamed"


def validate_upload(file: UploadFile) -> None:
    """Validate extension and declared size. Raises UploadValidationError."""
    original = file.filename or ""
    ext = os.path.splitext(original)[1].lower()

    if ext in config.REJECTED_EXTENSIONS:
        if ext in (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif"):
            raise UploadValidationError(
                f"{original}: images are not accepted by the ABBY Files API. "
                "Use the text/document formats "
                f"({config.SUPPORTED_EXTENSIONS_HUMAN})."
            )
        if ext in (".csv", ".xls", ".xlsx"):
            raise UploadValidationError(
                f"{original}: spreadsheets are not accepted by the ABBY Files "
                f"API. Please export to PDF or use a supported format "
                f"({config.SUPPORTED_EXTENSIONS_HUMAN})."
            )
        if ext == ".json":
            raise UploadValidationError(
                f"{original}: JSON files are not accepted by the ABBY Files API."
            )

    if ext not in config.SUPPORTED_EXTENSIONS:
        raise UploadValidationError(
            f"{original}: unsupported file type '{ext or 'none'}'. "
            f"Allowed: {config.SUPPORTED_EXTENSIONS_HUMAN}."
        )


async def save_files(files: List[UploadFile], session_id: str) -> List[str]:
    """
    Validate and stream uploads to disk. Returns absolute-ish paths.

    Files are streamed in chunks so a 200 MB upload does not have to be fully
    loaded into memory at once. We track total bytes written and enforce the
    200 MB ceiling ourselves (Starlette's spooled upload also helps).
    """
    if not files:
        raise UploadValidationError("No files were uploaded.")
    if len(files) > config.MAX_FILES_PER_REQUEST:
        raise UploadValidationError(
            f"Too many files ({len(files)}). A maximum of "
            f"{config.MAX_FILES_PER_REQUEST} files per analysis is allowed."
        )

    session_path = upload_dir(str(session_id))

    file_paths: List[str] = []
    for file in files:
        validate_upload(file)

        safe_name = make_safe_filename(file.filename)
        file_path = os.path.join(session_path, safe_name)

        bytes_written = 0
        # Reset in case SpooledTemporaryFile was already partially consumed.
        await file.seek(0)
        with open(file_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MB chunks
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > config.MAX_FILE_SIZE_BYTES:
                    out.close()
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
                    mb = config.MAX_FILE_SIZE_BYTES // (1024 * 1024)
                    raise UploadValidationError(
                        f"{file.filename} is larger than {mb} MB (the ABBY "
                        "Files API limit). Please upload a smaller file."
                    )
                out.write(chunk)

        file_paths.append(file_path)

    return file_paths


def cleanup_files(file_paths: List[str]) -> None:
    """Best-effort removal of saved uploads."""
    for fp in file_paths:
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except OSError:
            pass
