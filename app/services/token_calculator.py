"""
Token estimation utilities.

The ABBY platform bills tokens uniformly across every model it supports:

    Estimated Tokens = Total Characters / 4

This applies to BOTH input (prompt + document content) and output (the
model's answer). See "Rate Limits > Token Calculation" in the ABBY docs.

IMPORTANT for the Files API workflow: when you reference an uploaded file by
`file_id`, ABBY injects the EXTRACTED MARKDOWN into the chat context -- NOT the
raw PDF/DOCX bytes. Estimating from raw file size (e.g. 15 MB / 4 = 3.7M
tokens) over-counts by 50-100x because PDFs are compressed binary. The
accurate approach is to fetch the parsed markdown with
GET /files/{id}/content?output=markdown and measure its character count.
"""
from typing import Iterable, Optional

# Universal ABBY rule: 1 token ~= 4 characters.
CHARS_PER_TOKEN = 4


def estimate_text_tokens(text: Optional[str]) -> int:
    """Tokens for a piece of text using the ABBY chars/4 rule."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_markdown_tokens(markdown: Optional[str]) -> int:
    """Tokens for a document's extracted markdown text."""
    return estimate_text_tokens(markdown)


def estimate_request_tokens(prompt: str,
                            markdown_tokens: int = 0,
                            output_reserve: int = 0,
                            extra_text: Iterable[str] = ()) -> int:
    """
    Total tokens to reserve for one chat request (input + expected output).

    Args:
        prompt: the prompt text we are about to send.
        markdown_tokens: pre-measured tokens for the extracted document
            markdown (fetch it with files_service.get_markdown()).
        output_reserve: tokens reserved for the model's reply.
        extra_text: any other strings included in the request (e.g. JSON
            requirement payloads), each counted at chars/4.

    Reserving output tokens up front matters because the per-minute limit
    counts prompt tokens AND completion tokens together.
    """
    total = estimate_text_tokens(prompt) + int(markdown_tokens) + int(output_reserve)
    for t in extra_text or ():
        total += estimate_text_tokens(t)
    return total


def human_tokens(n: int) -> str:
    """Format a token count compactly, e.g. 12.3K."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
