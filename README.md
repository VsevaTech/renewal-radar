# Renewal Radar

**Renewal Radar is an assistant, not legal advice.
Always verify extracted contract terms against the original document.**

Upload a contract, an invoice or paste the text. Renewal Radar finds the
automatic-renewal clause, quotes the exact sentence it relied on, and tells you the
**last safe day to cancel**.

This is not a subscription tracker. The value is in reading the terms out of the
document you already have.

```
document → detect renewal → detect notice period → calculate cancel-by date
        → user confirms → export reminder
```

### Worked example

Input:

```
The agreement renews automatically on 15 November 2026
unless cancelled at least 30 days before renewal.
```

Output:

```
Provider:       Example SaaS
Renewal date:   15 Nov 2026
Notice period:  30 days
Cancel by:      16 Oct 2026
Source quote:   "The agreement renews automatically on 15 November 2026
                 unless cancelled at least 30 days before renewal."
```

---

## The one rule that matters

**The AI never computes the deadline.**

The model is asked only to *read*: provider, whether renewal is automatic, the renewal
date, the notice period, the billing period, the amount and currency if stated, and a
verbatim `source_quote` that backs it up. Its output is validated against a Pydantic
schema that has no `cancel_by` field at all.

The deadline itself is plain Python:

```python
cancel_by = renewal_date - timedelta(days=notice_period_days)
```

It lives in [`app/services/dates.py`](app/services/dates.py), is deterministic, and is
covered by unit tests including leap-year and year-boundary arithmetic.

**The model is also never allowed to invent a date.** If the document has no
unambiguous renewal date, or the date cannot be pinned down, the UI shows:

```
Needs confirmation
```

…together with the reason, and an editable form so you can supply the value yourself.

---

## AI provider

Renewal Radar uses the **Google Gemini API free tier** (Gemini Developer API /
Google AI Studio) through the official [`google-genai`](https://github.com/googleapis/python-genai)
Python SDK. A free key from <https://aistudio.google.com/apikey> is enough to run this
project — **no paid AI API is required**, and none is used. There is no dependency on
OpenAI, Anthropic or any other commercial AI API.

Two environment variables control it, and nothing else in the codebase hardcodes a
model id:

```env
GEMINI_API_KEY=
GEMINI_MODEL=
```

* `GEMINI_API_KEY` — your free AI Studio key. Leave it empty and the app still runs, in
  manual-entry-only mode, with no document text leaving your machine.
* `GEMINI_MODEL` — the Flash model to call, for example `gemini-3.8-flash`. Pick any
  current Flash model available on your free tier; check
  <https://ai.google.dev/gemini-api/docs/models> for what is live today. When the
  variable is empty, the single fallback constant `DEFAULT_GEMINI_MODEL` in
  [`app/config.py`](app/config.py) is used.

### Swapping the provider

All vendor code sits behind the `RenewalExtractor` interface in
[`app/services/ai.py`](app/services/ai.py). To move to another provider, add one class
implementing `async def extract(text) -> RenewalExtraction` and return it from
`build_extractor()`. No route, template or date-maths code changes.

### Retries

Gemini answers **503 UNAVAILABLE** when a model is busy, which is common on the free
tier for the newest Flash models. Transient upstream failures (5xx and upstream
timeouts) are retried with exponential backoff — `AI_MAX_ATTEMPTS`, default 3 — while
`AI_TIMEOUT_SECONDS` bounds the whole extraction, retries included.

Quota and rate-limit answers (429) are **never** retried: retrying a rate-limited
free-tier key only burns the remaining allowance faster. If 503s persist, set
`GEMINI_MODEL` to a less busy Flash model.

### When the AI is unavailable

Quota exhausted, rate-limited, timed out, offline, or answering with malformed JSON —
every one of those is caught and turned into:

```
AI extraction is temporarily unavailable.
You can retry later or enter renewal details manually.
```

The app does not crash, the document is kept, a **Retry** button is offered, and the
manual entry form stays fully usable. Renewal Radar is useful with no AI at all.

---

## Privacy

> **When AI extraction is enabled, the text of your document is sent to an external AI
> provider (Google) for processing.** Contracts and invoices frequently contain
> commercial and personal data. Decide accordingly.

What the project does and does not do:

* Document text is **never written to the logs** — only the character count and file
  type are logged.
* The `GEMINI_API_KEY` is **never logged**, never stored in the database, and never
  included in an error message shown to the user.
* The API key is read from the environment only. `.env` is git-ignored and
  `.env.example` ships with empty values.
* Extracted fields and the document text are stored **locally** in SQLite
  (`DATABASE_PATH`, default `./data/renewal_radar.db`) so you can revisit and correct an
  extraction. Every document has a **Delete** button, and deleting removes the row.
* Set `GEMINI_API_KEY=` (empty) to run fully offline: nothing is sent anywhere.
* No telemetry, no analytics, no third-party calls other than the AI provider.

---

## Run it

### Docker (recommended)

```bash
cp .env.example .env
# put your free AI Studio key in GEMINI_API_KEY and a Flash model id in GEMINI_MODEL
docker compose up --build
```

Open <http://localhost:8000>.

### Local Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # then fill in GEMINI_API_KEY / GEMINI_MODEL
uvicorn app.main:app --reload
```

### Check your key and model once, live

```bash
GEMINI_API_KEY=... python scripts/live_check.py
```

It prints the Flash models your key can actually see, runs one real extraction on
`examples/saas-contract.txt`, and asserts the deterministic answer (`16 Oct 2026`).
This script is deliberately **not** part of `pytest` or CI.

### Try it without a key

```bash
uvicorn app.main:app --reload
# paste examples/saas-contract.txt into the form, then fill the fields manually
```

---

## Demo

1. Upload `examples/saas-contract.txt`.
2. Gemini detects the automatic renewal on **15 Nov 2026**.
3. It detects the **30-day** notice period.
4. The **source quote** is shown verbatim from the document.
5. Deterministic Python computes **cancel by 16 Oct 2026**.
6. You confirm the details (or correct them first).
7. You download `reminder.ics` — an all-day event on the cancel-by date with a 7-day
   advance alarm.

Three synthetic documents are bundled:

| File | What it exercises |
| --- | --- |
| `examples/saas-contract.txt` | annual SaaS agreement, explicit date, 30-day notice |
| `examples/insurance-renewal.txt` | insurance renewal notice, 60-day notice, EUR premium |
| `examples/service-agreement.txt` | agreement that does **not** renew automatically |

---

## Supported inputs

PDF, DOCX, plain text, and pasted text (10 MB limit). Scanned/image-only PDFs are not
OCR'd — paste the relevant text instead.

Extracted per document: vendor/provider, renewal date, billing period, auto-renewal
yes/no, notice period, amount and currency when present, and the source quote. Every
field is manually correctable.

---

## Tests

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

**The Gemini API is mocked throughout the suite — CI never needs a real API key.**
GitHub Actions runs lint, format check and the full suite on Python 3.11 and 3.12, and
separately builds the Docker image and smoke-tests the running container.
Covered: fixed renewal date, 30-day notice, 60-day notice, annual renewal, no
auto-renewal, ambiguous date, missing date, leap-year and year-boundary arithmetic,
malformed AI responses, AI timeouts, AI quota / rate-limit responses, retry of
transient 503s, and that the app stays usable through all of them.

---

## Project layout

```
app/
  config.py              settings; the only default model id in the codebase
  schemas.py             Pydantic contracts (no cancel_by field — by design)
  main.py                FastAPI routes
  services/
    ai.py                provider-agnostic AI layer + Gemini implementation
    dates.py             deterministic cancel-by maths
    documents.py         PDF / DOCX / text extraction
    calendar.py          .ics reminder export
    extraction.py        merging manual corrections onto an extraction
    storage.py           local SQLite persistence
  templates/, static/    Jinja2 + HTMX UI
tests/                   pytest suite, Gemini fully mocked
examples/                synthetic contracts
```

## Licence

MIT — see [LICENSE](LICENSE).
