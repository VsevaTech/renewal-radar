"""The .ics reminder export."""

from __future__ import annotations

import datetime as dt

import pytest
from icalendar import Calendar

from app.schemas import RenewalExtraction
from app.services.calendar import CalendarError, build_reminder, safe_filename
from app.services.dates import assess


def _calendar(extraction: RenewalExtraction) -> Calendar:
    return Calendar.from_ical(build_reminder(assess(extraction, today=dt.date(2026, 9, 14))))


def test_reminder_is_an_all_day_event_on_the_cancel_by_date(saas_extraction):
    event = _calendar(saas_extraction).walk("VEVENT")[0]
    assert event["DTSTART"].dt == dt.date(2026, 10, 16)
    assert event["DTEND"].dt == dt.date(2026, 10, 17)


def test_reminder_summary_names_the_provider(saas_extraction):
    event = _calendar(saas_extraction).walk("VEVENT")[0]
    assert "Example SaaS" in str(event["SUMMARY"])


def test_reminder_description_carries_the_source_quote(saas_extraction):
    description = str(_calendar(saas_extraction).walk("VEVENT")[0]["DESCRIPTION"])
    assert "30 days before renewal" in description
    assert "Notice period: 30 days" in description
    assert "Cancel by: 16 Oct 2026" in description


def test_reminder_repeats_the_not_legal_advice_disclaimer(saas_extraction):
    description = str(_calendar(saas_extraction).walk("VEVENT")[0]["DESCRIPTION"])
    assert "Renewal Radar is an assistant, not legal advice." in description


def test_reminder_has_a_lead_time_alarm(saas_extraction):
    alarms = _calendar(saas_extraction).walk("VALARM")
    assert len(alarms) == 1
    assert alarms[0]["TRIGGER"].dt == dt.timedelta(days=-7)


def test_reminder_uid_is_stable(saas_extraction):
    first = _calendar(saas_extraction).walk("VEVENT")[0]["UID"]
    second = _calendar(saas_extraction).walk("VEVENT")[0]["UID"]
    assert first == second


def test_reminder_requires_a_computed_deadline():
    extraction = RenewalExtraction(auto_renewal=True, notice_period_days=30)
    with pytest.raises(CalendarError):
        build_reminder(assess(extraction))


def test_filename_is_filesystem_safe():
    name = safe_filename("Example SaaS / Ltd.", dt.date(2026, 10, 16))
    assert name == "example-saas-ltd.-cancel-by-2026-10-16.ics"
    assert "/" not in name


def test_filename_falls_back_without_a_provider():
    assert safe_filename(None, dt.date(2026, 10, 16)) == "renewal-cancel-by-2026-10-16.ics"
