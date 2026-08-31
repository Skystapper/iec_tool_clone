# IEC 62443 Compliance Analysis Tool

Analyzes security documents against IEC 62443 requirements using the **ABBY
Developer API**. It extracts document text (via the ABBY Files API), evaluates
each requirement with an LLM, and produces an Excel compliance report.

## What was fixed

The previous version inlined every document as base64 inside each chat call,
guessed token cost as "10 pages × 250 tokens", blocked the event loop with
`sleep`, capped uploads at 10 MB/PDF-only, and retried without jitter. It was
reworked to follow the ABBY documentation (https://docs.abby.abb.com/):

### Context / large documents
* Uses the **ABBY Files API** (`POST /files` → poll until `success` → reference
  by `file_id`) instead of inlining base64. Inline `input_file` is **Gemini /
  PDF-only** per the docs and forces the whole raw file into the prompt
  context; the Files API injects pre-extracted markdown.
* Each document is **uploaded once and reused across every requirement** (e.g.
  one file × 18 SG requirements = 1 upload, 18 chat calls instead of 18
  multi-MB base64 payloads).
* Model is configurable. Defaults to `gpt-5-nano`, a fast model recommended by
  the docs for processing large amounts of text; switch to `abby_auto`,
  `gemini-3.5-flash`, `claude-4.5-haiku`, etc. via `.env`. (The original
  hardcoded an agent/Claude model that produced "Not Accessed" errors — see
  `ERROR ANCOUNTERED.docx`.)
* Context-limit errors are caught and returned as a clear result row instead of
  crashing.

### Rate limits (100,000 tokens/minute, rolling window)
* Token estimation follows the documented rule **`tokens = characters / 4`**,
  measured from the **real file size** and prompt, plus a configurable reserve
  for the **model output** (ABBY bills both).
* An **async token bucket** paces requests *before* they are sent, running at
  90% of the hard cap (safety margin) so estimation error or other clients
  sharing the key don't trigger 429s. It uses `asyncio.sleep`, so it never
  blocks the event loop.
* After each call the bucket is **reconciled with the API's real `usage`**
  when it is returned.
* On any 429 that still slips through, the client honours `Retry-After` and
  retries with **exponential backoff + full jitter** (5 attempts for 429, 3 for
  500/502/503). 4xx client errors are not retried.
* Files are **deleted from ABBY after each run** so the 100-files/application
  cap is never reached.

### File uploads
* Server- and client-side limits match ABBY: **200 MB per file**, up to **10
  files per request**.
* Accepts everything the ABBY Files API accepts: `.pdf .docx .pptx .txt .md
  .html .htm .css .log` plus common code extensions and `.eml/.msg`.
* Rejects what ABBY rejects (images, `.csv/.xls/.xlsx`, `.json`) with a clear
  message instead of an opaque 400.
* Uploads are **streamed to disk in 1 MB chunks** (no full-file buffering) and
  the 200 MB ceiling is enforced while writing.

## Project structure

```
main.py                       # FastAPI app, lifespan, /health, /test-config
.env.example                  # copy to .env and add your ABBY_API_KEY
requirements.txt
app/
  core/
    config.py                 # all settings / limits from env
    rate_limiter.py           # async token bucket (100k/min, 90% safety)
    retry_handler.py          # async retries, jitter, Retry-After, classifiers
    file_manager.py           # streaming save + validation (200MB, types)
    error_handling.py         # backwards-compatible re-exports
    logging_config.py         # rotating file + console logs
    session_manager.py
  routes/
    analyze.py                # /api/analyze orchestration (upload once, reuse)
  services/
    abby_client.py            # async simple_chat/agent_chat client
    files_service.py          # async ABBY Files API (upload/poll/delete)
    token_calculator.py       # chars/4 estimation from real sizes
    prompt_service.py         # builds the per-requirement prompt
    parser_service.py         # parses Status/Explanation/Evidence
    excel_service.py          # reads data/SDLC.xlsx
    report_service.py         # writes the Excel report
    ai_service1.py            # legacy sync wrapper (kept for compatibility)
data/SDLC.xlsx                # requirements database
templates/index2 - Currently using.html
tests/test_e2e_mock.py        # end-to-end test against a mock ABBY server
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit .env and set ABBY_API_KEY
```

### Minimum `.env`

```ini
ABBY_API_KEY=your-key
ABBY_CHAT_MODE=simple              # or "agent"
ABBY_MODEL=gpt-5-nano              # used when mode=simple
# ABBY_AGENT_ID=...                # required when mode=agent
```

Run:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

Check `http://localhost:8000/test-config` for the live configuration and rate
bucket state.

## Choosing a model

Per the ABBY docs, use a **fast, cheap model** for processing/compliance
scanning of documents: `gpt-5-nano`, `gpt-5-mini`, `gemini-3-flash`,
`gemini-3.5-flash`, `claude-4.5-haiku`, or `abby_auto` (automatic routing).
Reserve heavier models (e.g. `claude-4.6-sonnet`, `gpt-5.4`) for difficult
reasoning. Note that advanced models may require **reviewed application
credentials** rather than the default user key.

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_TOKENS_PER_MINUTE` | `100000` | ABBY per-minute cap |
| `RATE_LIMIT_SAFETY_FACTOR` | `0.9` | Stay at 90% of the cap |
| `OUTPUT_TOKEN_RESERVE` | `4000` | Tokens reserved for each answer |
| `MAX_FILE_SIZE_BYTES` | `209715200` | 200 MB (ABBY limit) |
| `MAX_FILES_PER_REQUEST` | `10` | Per-analysis file cap |
| `FILE_POLL_MAX_WAIT_SECONDS` | `600` | How long to wait for OCR/parsing |
| `DELETE_FILES_AFTER_ANALYSIS` | `true` | Clean up ABBY files after each run |
| `ABBY_READ_TIMEOUT` | `300` | Per-request network timeout |

## Testing

```bash
python tests/test_e2e_mock.py
```

This spins up a mock ABBY server and verifies that a category run uploads the
file **once**, reuses it for every requirement, deletes it afterwards, and
produces a downloadable Excel report.
