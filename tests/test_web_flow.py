"""The user-facing flow: upload → detect → confirm → export, plus AI-failure fallbacks."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from icalendar import Calendar

from app.schemas import BillingPeriod, RenewalExtraction
from app.services.ai import (
    AI_UNAVAILABLE_MESSAGE,
    AIQuotaError,
    AIResponseError,
    AITimeoutError,
)

from .conftest import FakeExtractor

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAAS = (EXAMPLES / "saas-contract.txt").read_text()


def _extract(client, text: str = SAAS):
    response = client.post("/extract", data={"pasted_text": text}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    return location, client.get(location)


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"


def test_index_renders(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Upload a contract or invoice" in page.text


def test_disclaimer_is_on_every_page(client):
    assert "not legal advice" in client.get("/").text


# --------------------------------------------------------------------------------------
# the demo flow
# --------------------------------------------------------------------------------------


def test_paste_flow_shows_cancel_by_and_source_quote(client):
    _, page = _extract(client)
    assert page.status_code == 200
    assert "Example SaaS" in page.text
    assert "15 Nov 2026" in page.text
    assert "16 Oct 2026" in page.text  # deterministic cancel-by
    assert "30 days" in page.text
    assert "Source quote" in page.text
    assert "30 days before renewal" in page.text


def test_file_upload_flow(client):
    response = client.post(
        "/extract",
        files={"document": ("saas-contract.txt", SAAS.encode(), "text/plain")},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "16 Oct 2026" in response.text


def test_confirm_then_download_ics(client):
    location, _ = _extract(client)
    confirmed = client.post(f"{location}/confirm")
    assert confirmed.status_code == 200
    assert "Download reminder.ics" in confirmed.text

    ics = client.get(f"{location}/reminder.ics")
    assert ics.status_code == 200
    assert ics.headers["content-type"].startswith("text/calendar")
    assert "example-saas-cancel-by-2026-10-16.ics" in ics.headers["content-disposition"]

    event = Calendar.from_ical(ics.content).walk("VEVENT")[0]
    assert event["DTSTART"].dt == dt.date(2026, 10, 16)


def test_ics_is_refused_without_a_deadline(make_client):
    client = make_client(FakeExtractor(result=RenewalExtraction(auto_renewal=True)))
    location, _ = _extract(client)
    assert client.get(f"{location}/reminder.ics").status_code == 409


def test_unknown_extraction_is_404(client):
    assert client.get("/e/does-not-exist").status_code == 404


def test_delete_removes_the_document(client):
    location, _ = _extract(client)
    assert client.post(f"{location}/delete", follow_redirects=False).status_code == 303
    assert client.get(location).status_code == 404


# --------------------------------------------------------------------------------------
# needs confirmation
# --------------------------------------------------------------------------------------


def test_missing_date_shows_needs_confirmation(make_client):
    client = make_client(
        FakeExtractor(result=RenewalExtraction(auto_renewal=True, notice_period_days=30))
    )
    _, page = _extract(client)
    assert "Needs confirmation" in page.text
    assert "No unambiguous renewal date" in page.text


def test_ambiguous_date_shows_needs_confirmation(make_client):
    client = make_client(
        FakeExtractor(
            result=RenewalExtraction(
                auto_renewal=True,
                renewal_date=dt.date(2027, 1, 1),
                renewal_date_ambiguous=True,
                notice_period_days=30,
            )
        )
    )
    _, page = _extract(client)
    assert "Needs confirmation" in page.text
    assert "ambiguous" in page.text


def test_no_auto_renewal_document(make_client):
    client = make_client(
        FakeExtractor(
            result=RenewalExtraction(
                provider="Meridian Facilities Services LLC",
                auto_renewal=False,
                source_quote="shall terminate and shall not renew automatically",
            )
        )
    )
    _, page = _extract(client, (EXAMPLES / "service-agreement.txt").read_text())
    assert "No automatic renewal" in page.text
    assert "no cancellation deadline" in page.text


def test_60_day_notice_document(make_client):
    client = make_client(
        FakeExtractor(
            result=RenewalExtraction(
                provider="Northstar Insurance Group",
                auto_renewal=True,
                renewal_date=dt.date(2027, 3, 1),
                notice_period_days=60,
                billing_period=BillingPeriod.ANNUAL,
                amount=8940.0,
                currency="EUR",
                source_quote="you must notify us no later than 60 days prior to the renewal date",
            )
        )
    )
    _, page = _extract(client, (EXAMPLES / "insurance-renewal.txt").read_text())
    assert "31 Dec 2026" in page.text
    assert "60 days" in page.text


# --------------------------------------------------------------------------------------
# AI failures never crash the app
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        AITimeoutError("timeout"),
        AIQuotaError("quota"),
        AIResponseError("malformed"),
        RuntimeError("unexpected"),
    ],
    ids=["timeout", "quota", "malformed", "unexpected"],
)
def test_ai_failure_shows_the_required_message_and_manual_form(make_client, error):
    client = make_client(FakeExtractor(error=error))
    _, page = _extract(client)
    assert page.status_code == 200
    for line in AI_UNAVAILABLE_MESSAGE.split("\n"):
        assert line in page.text
    assert "Correct these details manually" in page.text
    assert "Needs confirmation" in page.text


def test_app_runs_without_any_ai_configured(make_client):
    client = make_client(None)
    assert "manual entry only" in client.get("/").text
    _, page = _extract(client)
    assert "not configured" in page.text
    assert "Correct these details manually" in page.text


def test_manual_entry_rescues_a_failed_extraction(make_client):
    client = make_client(FakeExtractor(error=AIQuotaError("quota")))
    location, _ = _extract(client)

    edited = client.post(
        f"{location}/edit",
        data={
            "provider": "Example SaaS",
            "auto_renewal": "yes",
            "renewal_date": "2026-11-15",
            "notice_period_days": "30",
            "billing_period": "annual",
            "amount": "14400",
            "currency": "usd",
            "source_quote": "unless cancelled at least 30 days before renewal",
        },
    )
    assert edited.status_code == 200
    assert "16 Oct 2026" in edited.text
    assert "USD" in edited.text

    client.post(f"{location}/confirm")
    ics = client.get(f"{location}/reminder.ics")
    assert ics.status_code == 200
    assert b"Example SaaS" in ics.content


def test_retry_after_a_failure_succeeds(make_client, saas_extraction):
    extractor = FakeExtractor(error=AIQuotaError("quota"))
    client = make_client(extractor)
    location, page = _extract(client)
    assert "Retry AI extraction" in page.text

    extractor.error = None
    extractor.result = saas_extraction
    retried = client.post(f"{location}/retry")
    assert retried.status_code == 200
    assert "16 Oct 2026" in retried.text


def test_manual_edit_rejects_a_bad_date(client):
    location, _ = _extract(client)
    response = client.post(f"{location}/edit", data={"renewal_date": "15/11/2026"})
    assert response.status_code == 422


def test_manual_edit_rejects_a_negative_notice_period(client):
    location, _ = _extract(client)
    response = client.post(f"{location}/edit", data={"notice_period_days": "-5"})
    assert response.status_code == 422


def test_manual_edit_can_clear_a_hallucinated_date(client):
    """The user must always be able to take a date away, not only add one."""
    location, page = _extract(client)
    assert "16 Oct 2026" in page.text
    cleared = client.post(
        f"{location}/edit",
        data={"provider": "Example SaaS", "auto_renewal": "yes", "renewal_date": ""},
    )
    assert "Needs confirmation" in cleared.text


def test_htmx_request_returns_only_the_card(client):
    location, _ = _extract(client)
    response = client.post(
        f"{location}/edit",
        data={"renewal_date": "2026-11-15", "notice_period_days": "30", "auto_renewal": "yes"},
        headers={"HX-Request": "true"},
    )
    assert "<!DOCTYPE html>" not in response.text
    assert "16 Oct 2026" in response.text


def test_empty_paste_is_rejected_politely(client):
    response = client.post("/extract", data={"pasted_text": "   "})
    assert response.status_code == 400
    assert "Paste some contract text first." in response.text


def test_unsupported_upload_is_rejected_politely(client):
    response = client.post(
        "/extract", files={"document": ("scan.png", b"\x89PNG\r\n", "image/png")}
    )
    assert response.status_code == 400
    assert "Unsupported file type" in response.text
