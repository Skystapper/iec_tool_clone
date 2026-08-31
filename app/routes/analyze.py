"""
Analysis endpoint: orchestrates upload -> ABBY Files API -> per-requirement
analysis -> Excel report.

Key improvements over the original:
  * Each uploaded document is sent to the ABBY Files API ONCE and the returned
    file_id is reused for every requirement (the old code re-encoded and
    re-sent the whole base64 file per requirement).
  * Token cost is estimated from real file sizes (chars/4 rule) and a fixed
    output reserve, then the async bucket paces requests under 100k/min.
  * Files are deleted from ABBY afterwards so the 100-files/app cap is never
    reached.
  * All API/network work is async and concurrent.
"""
import asyncio
import logging
import os
import traceback
from typing import List

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.core import config
from app.core.file_manager import (
    UploadValidationError,
    cleanup_files,
    save_files,
)
from app.core.paths import output_report_path
from app.core.rate_limiter import TokenRequestTooLarge
from app.services import files_service
from app.services.abby_client import analyze as abby_analyze
from app.services.excel_service import (
    get_all_requirements_for_category,
    get_requirement_data,
)
from app.services.parser_service import parse_ai_output, parse_batch_output
from app.services.prompt_service import build_batch_prompt, build_prompt
from app.services.report_service import generate_excel
from app.services.token_calculator import (
    estimate_request_tokens,
    estimate_text_tokens,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _error_result(category: str, req: dict, file_name: str,
                  explanation: str, status: str = "Error") -> dict:
    return {
        "file_name": file_name,
        "category": category,
        "id": req["id"],
        "full_id": req["full_id"],
        "requirement": req["requirement"],
        "rationale": req["rationale"],
        "guidance": req["guidance"],
        "status": status,
        "explanation": explanation,
        "evidence": "",
        "recommendations": "",
    }


def _friendly_error(outcome: dict) -> str:
    """Turn an abby_client.analyze failure into a user-facing message."""
    kind = outcome.get("error_kind")
    msg = outcome.get("error", "AI analysis failed")
    if kind == "context_limit":
        return ("Document too large for the model's context window. Use a "
                "shorter document or a model with a larger context window.")
    if kind == "rate_limit":
        return ("ABBY rate limit (100,000 tokens/min) reached; please retry "
                "in a minute.")
    if kind == "server":
        return f"ABBY service error: {msg}"
    if kind == "empty":
        return "ABBY returned an empty response. Please retry."
    return msg


@router.post("/analyze")
async def analyze(
    requirement_id: str = Form(...),
    files: List[UploadFile] = File(...),
):
    logger.info("Starting analysis for requirement_id=%s, %d file(s)",
                requirement_id, len(files) if files else 0)

    # ----- 1. Validate & save uploads FIRST (no API key needed) -----------
    try:
        session_id = requirement_id.replace("/", "_").replace("\\", "_")
        file_paths = await save_files(files, session_id)
    except UploadValidationError as e:
        logger.warning("Upload validation failed: %s", e)
        return {"success": False, "message": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to save uploads: %s\n%s", e, traceback.format_exc())
        return {"success": False, "message": f"Could not save uploads: {e}"}

    if not config.is_configured():
        cleanup_files(file_paths)
        return {
            "success": False,
            "message": "ABBY is not configured: "
                       + config.missing_config_message(),
        }

    try:
        # ----- 2. Resolve requirements ------------------------------------
        if "-" in requirement_id:
            category, req_id = requirement_id.split("-", 1)
            category = category.upper()
            req_data = get_requirement_data(category, req_id)
            if req_data is None:
                return {"success": False,
                        "message": f"Requirement {requirement_id} not found"}
            requirements = [{
                "id": req_id,
                "full_id": requirement_id,
                **req_data,
            }]
        else:
            category = requirement_id.upper()
            requirements = get_all_requirements_for_category(category)
            if not requirements:
                return {"success": False,
                        "message": f"No requirements found for {category}"}

        logger.info("Analysing %d requirement(s) across %d file(s)",
                    len(requirements), len(file_paths))

        # ----- 3. Upload each file ONCE to the ABBY Files API -------------
        # We create one shared httpx client for the whole analysis run.
        client = files_service.new_client()

        file_id_map = {}  # path -> file_id
        try:
            upload_tasks = [
                files_service.upload_and_wait(client, fp) for fp in file_paths
            ]
            uploaded = await asyncio.gather(*upload_tasks, return_exceptions=True)
            for fp, res in zip(file_paths, uploaded):
                if isinstance(res, Exception):
                    # Log the FULL exception (type + repr + traceback) so a
                    # network error with an empty message is still diagnosable.
                    logger.error(
                        "Upload failed for %s -> %s: %r",
                        fp, type(res).__name__, res, exc_info=res,
                    )
                    return {
                        "success": False,
                        "message": f"Could not upload {os.path.basename(fp)} "
                                   f"to ABBY: {res}",
                    }
                file_id_map[fp] = res

            # ----- 4. Fetch extracted markdown per file ------------------
            # With the Files API, ABBY injects the PARSED MARKDOWN (not the raw
            # PDF bytes) into context. Fetching it lets us (a) estimate token
            # cost accurately instead of guessing from the compressed PDF size,
            # and (b) fail fast if a document alone exceeds the per-minute cap.
            file_meta = {}  # path -> {file_id, markdown, md_tokens}
            for fp in file_paths:
                fid = file_id_map[fp]
                markdown = await files_service.get_markdown(client, fid)
                md_tokens = estimate_text_tokens(markdown)
                file_meta[fp] = {
                    "file_id": fid, "markdown": markdown, "md_tokens": md_tokens,
                }
                logger.info(
                    "%s: extracted markdown ~%d chars (~%d tokens)",
                    os.path.basename(fp), len(markdown), md_tokens,
                )

            # ----- 5. Analyse (batched) -----------------------------------
            # A category run has multiple requirements; per the docs we batch
            # them into ONE request per file so the document markdown is sent
            # only once (vs once per requirement). A single specific requirement
            # uses the simpler one-shot prompt.
            is_batch = len(requirements) > 1
            if is_batch:
                prompt = build_batch_prompt(requirements)
            else:
                prompt = build_prompt(requirements[0], requirements[0]["full_id"])

            # Output reserve scales with the number of answers we expect.
            output_reserve = 1000 + 500 * len(requirements)

            results = []

            async def _run_file(file_path: str) -> List[dict]:
                file_name = os.path.basename(file_path)
                meta = file_meta[file_path]
                est = estimate_request_tokens(
                    prompt=prompt,
                    markdown_tokens=meta["md_tokens"],
                    output_reserve=output_reserve,
                )
                logger.info(
                    "Analysing %s against %d requirement(s); estimated %d tokens",
                    file_name, len(requirements), est,
                )

                try:
                    outcome = await abby_analyze(
                        client, prompt, [meta["file_id"]], est,
                    )
                except TokenRequestTooLarge as e:
                    logger.error("Document too large: %s", e)
                    return [
                        _error_result(
                            category, r, file_name,
                            "Document is too large to analyse in a single "
                            "request (its extracted text exceeds the ABBY "
                            "per-minute token limit). Split it or use a shorter "
                            "document.",
                            status="Error",
                        )
                        for r in requirements
                    ]

                if not outcome["success"]:
                    label = _friendly_error(outcome)
                    return [_error_result(category, r, file_name, label)
                            for r in requirements]

                if is_batch:
                    parsed_by_id = parse_batch_output(
                        outcome["content"], requirements,
                    )
                    rows = []
                    for r in requirements:
                        parsed = parsed_by_id.get(r["full_id"], {
                            "status": "Error",
                            "explanation": "Missing assessment in AI response.",
                            "evidence": "",
                        })
                        rows.append({
                            "file_name": file_name,
                            "category": category,
                            "id": r["id"],
                            "full_id": r["full_id"],
                            "requirement": r["requirement"],
                            "rationale": r["rationale"],
                            "guidance": r["guidance"],
                            **parsed,
                        })
                    return rows

                # Single-requirement path.
                r = requirements[0]
                parsed = parse_ai_output(outcome["content"])
                return [{
                    "file_name": file_name,
                    "category": category,
                    "id": r["id"],
                    "full_id": r["full_id"],
                    "requirement": r["requirement"],
                    "rationale": r["rationale"],
                    "guidance": r["guidance"],
                    **parsed,
                }]

            # Run files concurrently but bounded (2 at a time) to stay polite.
            sem = asyncio.Semaphore(2)

            async def _guarded(fp):
                async with sem:
                    return await _run_file(fp)

            per_file = await asyncio.gather(
                *[_guarded(fp) for fp in file_paths]
            )
            for rows in per_file:
                results.extend(rows)

        finally:
            # ----- 5. Cleanup ABBY files + local uploads ------------------
            if config.DELETE_FILES_AFTER_ANALYSIS:
                await asyncio.gather(
                    *[files_service.delete_file(client, fid)
                      for fid in file_id_map.values()],
                    return_exceptions=True,
                )
            await client.aclose()

        cleanup_files(file_paths)

        # ----- 6. Generate report -----------------------------------------
        generate_excel(results, session_id)

        ok = sum(1 for r in results if r.get("status") not in
                 ("Error", "Not Assessed"))
        logger.info("Analysis complete: %d/%d successful", ok, len(results))

        return {
            "success": True,
            "status": "Completed",
            "requirements_analyzed": len(results),
            "successful": ok,
            "failed": len(results) - ok,
            "download_url": f"/api/download/{session_id}",
        }

    except Exception as e:  # noqa: BLE001
        logger.error("Analysis failed: %s\n%s", e, traceback.format_exc())
        cleanup_files(file_paths)
        return {"success": False, "message": str(e)}


@router.get("/download/{session_id}")
def download_report(session_id: str):
    file_path = output_report_path(session_id)
    if not file_path.exists():
        return {"error": "File not found"}
    return FileResponse(
        path=str(file_path),
        filename="compliance_report.xlsx",
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
    )
