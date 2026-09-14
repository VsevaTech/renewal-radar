"""FastAPI application: upload → extract → confirm → export reminder."""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import __version__
from app.config import Settings, get_settings
from app.schemas import (
    NEEDS_CONFIRMATION_LABEL,
    BillingPeriod,
    ExtractionStatus,
    ManualOverride,
    RenewalExtraction,
)
from app.services.ai import (
    AI_UNAVAILABLE_MESSAGE,
    AIError,
    AINotConfiguredError,
    RenewalExtractor,
    build_extractor,
)
from app.services.calendar import CalendarError, build_reminder, safe_filename
from app.services.dates import assess
from app.services.documents import DocumentError, parse_document, parse_pasted_text
from app.services.extraction import apply_override
from app.services.storage import ExtractionStore, StoredExtraction

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("renewal_radar")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = getattr(app.state, "settings", None) or get_settings()
    app.state.settings = settings
    app.state.store = ExtractionStore(settings.database_path)
    app.state.extractor = _safe_build_extractor(settings)
    yield
    app.state.store.close()


def _safe_build_extractor(settings: Settings) -> RenewalExtractor | None:
    """Build the provider, or return None so the app still boots without a key."""
    try:
        return build_extractor(settings)
    except AINotConfiguredError:
        logger.warning("GEMINI_API_KEY is not set — running in manual-entry-only mode.")
        return None
    except AIError as exc:
        logger.warning("AI provider unavailable at startup: %s", exc)
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Renewal Radar", version=__version__, lifespan=lifespan)
    if settings is not None:
        app.state.settings = settings
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    _register_routes(app)
    return app


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _store(request: Request) -> ExtractionStore:
    return request.app.state.store


def _extractor(request: Request) -> RenewalExtractor | None:
    return request.app.state.extractor


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _record_or_404(request: Request, extraction_id: str) -> StoredExtraction:
    record = _store(request).get(extraction_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Extraction not found.")
    return record


def _result_context(request: Request, record: StoredExtraction) -> dict[str, Any]:
    assessment = assess(record.extraction)
    return {
        "request": request,
        "record": record,
        "assessment": assessment,
        "extraction": record.extraction,
        "status": assessment.status,
        "ExtractionStatus": ExtractionStatus,
        "needs_confirmation_label": NEEDS_CONFIRMATION_LABEL,
        "billing_periods": list(BillingPeriod),
        "ai_available": _extractor(request) is not None,
        "ai_provider_name": getattr(_extractor(request), "name", "none"),
        "ai_model": _settings(request).resolved_model,
    }


def _render_result(
    request: Request, record: StoredExtraction, status_code: int = 200
) -> HTMLResponse:
    context = _result_context(request, record)
    template = "partials/result_card.html" if _is_htmx(request) else "result.html"
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _optional_date(value: str | None) -> dt.date | None:
    if not value or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Renewal date must be YYYY-MM-DD.") from exc


def _optional_int(value: str | None, field: str) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a whole number.") from exc
    if parsed < 0:
        raise HTTPException(status_code=422, detail=f"{field} must not be negative.")
    return parsed


def _optional_float(value: str | None, field: str) -> float | None:
    if value is None or not str(value).strip():
        return None
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} must be a number.") from exc


def _tristate(value: str | None) -> bool | None:
    mapping = {"yes": True, "true": True, "no": False, "false": False}
    return mapping.get((value or "").strip().lower())


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:  # noqa: C901 - route table, intentionally flat
    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "ai_configured": _extractor(request) is not None,
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "ai_available": _extractor(request) is not None,
                "ai_provider_name": getattr(_extractor(request), "name", "none"),
                "ai_model": _settings(request).resolved_model,
                "recent": _store(request).recent(5),
            },
        )

    @app.post("/extract", response_class=HTMLResponse)
    async def extract(
        request: Request,
        document: Annotated[UploadFile | None, Form()] = None,
        pasted_text: Annotated[str | None, Form()] = None,
    ) -> Response:
        try:
            if document is not None and document.filename:
                data = await document.read()
                parsed = parse_document(data, document.filename, document.content_type)
            else:
                parsed = parse_pasted_text(pasted_text or "")
        except DocumentError as exc:
            return templates.TemplateResponse(
                request,
                "index.html",
                {
                    "request": request,
                    "error": str(exc),
                    "pasted_text": pasted_text or "",
                    "ai_available": _extractor(request) is not None,
                    "ai_provider_name": getattr(_extractor(request), "name", "none"),
                    "ai_model": _settings(request).resolved_model,
                    "recent": _store(request).recent(5),
                },
                status_code=400,
            )

        extraction, source, ai_error = await _run_extraction(request, parsed.text)
        record = _store(request).save(
            filename=parsed.filename,
            kind=parsed.kind,
            document_text=parsed.text,
            extraction=extraction,
            source=source,
            ai_error=ai_error,
        )
        return RedirectResponse(url=f"/e/{record.id}", status_code=303)

    @app.get("/e/{extraction_id}", response_class=HTMLResponse)
    async def show(request: Request, extraction_id: str) -> HTMLResponse:
        record = _record_or_404(request, extraction_id)
        context = _result_context(request, record)
        return templates.TemplateResponse(request, "result.html", context)

    @app.post("/e/{extraction_id}/edit", response_class=HTMLResponse)
    async def edit(
        request: Request,
        extraction_id: str,
        provider: Annotated[str | None, Form()] = None,
        auto_renewal: Annotated[str | None, Form()] = None,
        renewal_date: Annotated[str | None, Form()] = None,
        notice_period_days: Annotated[str | None, Form()] = None,
        billing_period: Annotated[str | None, Form()] = None,
        amount: Annotated[str | None, Form()] = None,
        currency: Annotated[str | None, Form()] = None,
        source_quote: Annotated[str | None, Form()] = None,
    ) -> HTMLResponse:
        record = _record_or_404(request, extraction_id)
        try:
            override = ManualOverride(
                provider=provider,
                auto_renewal=_tristate(auto_renewal),
                renewal_date=_optional_date(renewal_date),
                notice_period_days=_optional_int(notice_period_days, "Notice period"),
                billing_period=BillingPeriod(billing_period or "unknown"),
                amount=_optional_float(amount, "Amount"),
                currency=currency,
                source_quote=source_quote,
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid values submitted.") from exc

        # A manual edit replaces the stored extraction outright: the form always carries
        # the full set of fields, so blank means "clear this field".
        merged = apply_override(RenewalExtraction(notes=record.extraction.notes), override)
        updated = _store(request).update(
            extraction_id, extraction=merged, source="manual", confirmed=False
        )
        assert updated is not None  # noqa: S101 - existence checked above
        return _render_result(request, updated)

    @app.post("/e/{extraction_id}/retry", response_class=HTMLResponse)
    async def retry(request: Request, extraction_id: str) -> HTMLResponse:
        record = _record_or_404(request, extraction_id)
        extraction, source, ai_error = await _run_extraction(request, record.document_text)
        updated = _store(request).update(
            extraction_id, extraction=extraction, source=source, confirmed=False, ai_error=ai_error
        )
        assert updated is not None  # noqa: S101
        return _render_result(request, updated)

    @app.post("/e/{extraction_id}/confirm", response_class=HTMLResponse)
    async def confirm(request: Request, extraction_id: str) -> HTMLResponse:
        record = _record_or_404(request, extraction_id)
        assessment = assess(record.extraction)
        if assessment.cancel_by is None:
            return _render_result(request, record, status_code=400)
        updated = _store(request).update(
            extraction_id,
            extraction=record.extraction,
            source=record.source,
            confirmed=True,
            ai_error=record.ai_error,
        )
        assert updated is not None  # noqa: S101
        return _render_result(request, updated)

    @app.get("/e/{extraction_id}/reminder.ics")
    async def reminder(request: Request, extraction_id: str) -> Response:
        record = _record_or_404(request, extraction_id)
        assessment = assess(record.extraction)
        try:
            payload = build_reminder(assessment)
        except CalendarError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        assert assessment.cancel_by is not None  # noqa: S101
        filename = safe_filename(record.extraction.provider, assessment.cancel_by)
        return Response(
            content=payload,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/e/{extraction_id}/delete")
    async def delete(request: Request, extraction_id: str) -> RedirectResponse:
        _store(request).delete(extraction_id)
        return RedirectResponse(url="/", status_code=303)


async def _run_extraction(
    request: Request, document_text: str
) -> tuple[RenewalExtraction, str, str | None]:
    """Call the provider, converting every failure into an empty extraction + message."""
    extractor = _extractor(request)
    if extractor is None:
        return RenewalExtraction(), "manual", AINotConfiguredError.user_message

    try:
        return await extractor.extract(document_text), "ai", None
    except AIError as exc:
        logger.warning("AI extraction failed: %s", type(exc).__name__)
        return RenewalExtraction(), "manual", getattr(exc, "user_message", AI_UNAVAILABLE_MESSAGE)
    except Exception as exc:  # noqa: BLE001 - last resort: a provider bug must not 500
        logger.warning("Unexpected AI failure: %s", type(exc).__name__)
        return RenewalExtraction(), "manual", AI_UNAVAILABLE_MESSAGE


app = create_app()
