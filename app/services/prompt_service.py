"""
Prompt building for IEC 62443 analysis.

We intentionally keep these prompts THIN: the heavy instructions (role,
strict grading rules, verbatim-evidence rules, recommendations grounded in the
ELSP template, SM-2 role comparison) already live in the agent's server-side
system prompt and knowledge base. Our job is only to tell it which
requirements to assess against the attached document and to request a
machine-parseable shape that preserves its full native output -- including
Quoted Evidence with page references and Recommendations.
"""
import json
from typing import Dict, List


def build_prompt(req_data: dict, req_name: str) -> str:
    """Single-requirement prompt (used for specific IDs like SR-1)."""
    return f"""Assess the attached user manual against this IEC 62443-4-1
requirement, using your standard methodology and your knowledge base.

Requirement ID: {req_name}

Requirement Description:
{req_data['requirement']}

Rationale:
{req_data['rationale']}

Guidance:
{req_data['guidance']}

Respond with ONLY a JSON object (no markdown fences, no commentary) with
exactly these keys:
  - "id": "{req_name}"
  - "status": one of "Fully Met", "Partially Met", "Not Met"
  - "explanation": concise justification
  - "evidence": list of exact verbatim sentences from the manual that support
    the assessment, each appended with its page reference as "[page X]" when
    known. Use an empty list if there is no relevant sentence.
  - "recommendations": if status is Partially Met or Not Met, concrete
    improvement recommendations grounded in the ELSP Cyber Security User Manual
    Template. Use an empty string when Fully Met.
"""


def build_batch_prompt(requirements: List[dict]) -> str:
    """
    Build a prompt that evaluates MANY requirements in one request, preserving
    the agent's full native fields (evidence with page refs, recommendations).
    """
    compact = [
        {
            "id": r["full_id"],
            "requirement": r["requirement"],
            "rationale": r["rationale"],
            "guidance": r["guidance"],
        }
        for r in requirements
    ]
    specs = json.dumps(compact, ensure_ascii=False, indent=2)

    return f"""Assess the attached user manual against EACH of the following
{len(requirements)} IEC 62443-4-1 requirements, using your standard methodology
and your knowledge base. Apply your strict grading rules: a requirement is
Fully Met only when every core aspect is explicitly and completely addressed
with direct verbatim evidence; Partially Met when some but not all aspects are
covered; Not Met when no relevant evidence exists.

For each requirement, quote sentences VERBATIM from the manual (preserve
punctuation) and append the page reference as "[page X]" when known. If the
status is Partially Met or Not Met, provide concrete recommendations grounded
in the 3HKR000004 ELSP Cyber Security User Manual Template.

Respond with ONLY a valid JSON array -- no markdown fences, no commentary --
containing exactly {len(requirements)} objects, in the same order as the
requirements below. Each object MUST have exactly these keys:
  - "id": the requirement id exactly as given
  - "status": "Fully Met" | "Partially Met" | "Not Met"
  - "explanation": concise justification
  - "evidence": an array of verbatim supporting sentences with page references
    (empty array when none)
  - "recommendations": a string with recommendations, or "" when Fully Met

Requirements:
{specs}

Return the JSON array now.
"""
