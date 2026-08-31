"""
Parsing of ABBY responses into structured compliance results.

Two shapes are supported:
  * the JSON array returned by build_batch_prompt() for category runs, and
  * the single-requirement JSON object for specific IDs.

Both preserve the agent's richer native fields: evidence is a list of verbatim
quotations (with page references), and recommendations is a string. The legacy
free-text "Status: ..." parser is kept as a fallback in case the model ignores
the JSON instruction.
"""
import json
import re
from typing import Dict, List, Optional

_VALID_STATUSES = {
    "fully met": "Fully Met",
    "partially met": "Partially Met",
    "not met": "Not Met",
    "not addressed": "Not Addressed",
    "not assessed": "Not Assessed",
    "error": "Error",
}


def _normalize_status(raw: Optional[str]) -> str:
    if not raw:
        return "Not Assessed"
    return _VALID_STATUSES.get(str(raw).strip().lower(), str(raw).strip())


def _clean(value) -> str:
    """Coerce a model field to a trimmed string, treating None/'None' as blank."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _stringify_evidence(value) -> str:
    """Evidence may arrive as a list of bullets or a plain string."""
    if value is None:
        return ""
    if isinstance(value, list):
        lines = []
        for item in value:
            text = str(item).strip()
            if text:
                # Keep the agent's verbatim quotations readable in Excel.
                lines.append(f"• {text}" if not text.startswith("•") else text)
        return "\n".join(lines)
    return str(value).strip()


def _normalize_item(item: dict, req: dict) -> dict:
    return {
        "status": _normalize_status(item.get("status")),
        "explanation": _clean(item.get("explanation")),
        "evidence": _stringify_evidence(item.get("evidence")),
        "recommendations": _clean(item.get("recommendations")),
    }


def _extract_json(content: str, open_ch: str, close_ch: str):
    """Pull the first JSON object/array out of a response, tolerating fences."""
    fence = re.search(r"```(?:json)?\s*(" + re.escape(open_ch) + r".*?"
                      + re.escape(close_ch) + r")\s*```", content, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = content.find(open_ch)
        end = content.rfind(close_ch)
        if start == -1 or end == -1 or end < start:
            return None
        candidate = content[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Batch (JSON array)
# ---------------------------------------------------------------------------
def parse_batch_output(content: Optional[str],
                       requirements: List[dict]) -> Dict[str, dict]:
    """Parse a batch JSON response into {full_id: {status, explanation, evidence, recommendations}}."""
    fallback = {
        r["full_id"]: {
            "status": "Error",
            "explanation": "Could not parse the AI response for this batch.",
            "evidence": "",
            "recommendations": "",
        }
        for r in requirements
    }
    if not content:
        return fallback

    arr = _extract_json(content, "[", "]")
    if not isinstance(arr, list):
        # Maybe the model returned a single object or free text.
        obj = _extract_json(content, "{", "}")
        if isinstance(obj, dict):
            arr = [obj]
        else:
            return fallback

    by_id: Dict[str, dict] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id", "")).strip()
        if rid:
            by_id[rid] = _normalize_item(item, {})

    for r in requirements:
        by_id.setdefault(r["full_id"], {
            "status": "Not Assessed",
            "explanation": "The AI did not return an assessment for this requirement.",
            "evidence": "",
            "recommendations": "",
        })
    return by_id


def parse_ai_output(content: Optional[str]) -> dict:
    """Parse a single-requirement response (JSON object preferred, text fallback)."""
    if content is None or not str(content).strip():
        return {"status": "Not Assessed", "explanation": "No content",
                "evidence": "", "recommendations": ""}

    obj = _extract_json(str(content), "{", "}")
    if isinstance(obj, dict):
        return {
            "status": _normalize_status(obj.get("status")),
            "explanation": _clean(obj.get("explanation")),
            "evidence": _stringify_evidence(obj.get("evidence")),
            "recommendations": _clean(obj.get("recommendations")),
        }

    # ----- legacy free-text fallback -----
    text = str(content).strip()
    status_match = re.search(r"Status:\s*([^\n]+)", text, re.IGNORECASE)
    status = _normalize_status(status_match.group(1)) if status_match else "Not Assessed"

    explanation_match = re.search(
        r"Explanation:\s*(.*?)(?:\n\s*(?:Quoted )?Evidence:|\n\s*Recommendations:|$)",
        text, re.DOTALL | re.IGNORECASE)
    explanation = explanation_match.group(1).strip() if explanation_match else ""

    evidence_match = re.search(
        r"(?:Quoted )?Evidence:\s*(.*?)(?:\n\s*Recommendations:|$)",
        text, re.DOTALL | re.IGNORECASE)
    if evidence_match:
        evidence = evidence_match.group(1).strip()
    else:
        quotes = re.findall(r'"([^"]+)"', text)
        evidence = "\n".join(f"• {q}" for q in quotes)

    rec_match = re.search(r"Recommendations:\s*(.*)$", text,
                          re.DOTALL | re.IGNORECASE)
    recommendations = rec_match.group(1).strip() if rec_match else ""

    return {
        "status": status,
        "explanation": explanation,
        "evidence": evidence,
        "recommendations": recommendations,
    }
