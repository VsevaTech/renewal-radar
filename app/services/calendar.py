"""Build the ``.ics`` reminder for a confirmed cancellation deadline."""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from icalendar import Alarm, Calendar, Event

from app.schemas import RenewalAssessment

PRODID = "-//Renewal Radar//renewal-radar//EN"
DEFAULT_LEAD_DAYS = 7

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


class CalendarError(ValueError):
    """Raised when there is nothing to put in a calendar."""


def _uid(assessment: RenewalAssessment) -> str:
    seed = "|".join(
        [
            assessment.extraction.provider or "unknown-provider",
            assessment.cancel_by.isoformat() if assessment.cancel_by else "",
            str(assessment.extraction.notice_period_days or ""),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"{digest}@renewal-radar"


def safe_filename(provider: str | None, cancel_by: dt.date) -> str:
    stem = _UNSAFE_FILENAME.sub("-", (provider or "renewal")).strip("-").lower() or "renewal"
    return f"{stem}-cancel-by-{cancel_by.isoformat()}.ics"


def build_reminder(
    assessment: RenewalAssessment,
    lead_days: int = DEFAULT_LEAD_DAYS,
    now: dt.datetime | None = None,
) -> bytes:
    """Return an all-day VEVENT on the cancel-by date with a lead-time alarm.

    Raises :class:`CalendarError` when the assessment has no computed deadline —
    a reminder is only ever generated from deterministic maths.
    """
    if assessment.cancel_by is None:
        raise CalendarError("There is no confirmed cancel-by date to remind you about.")

    extraction = assessment.extraction
    provider = extraction.provider or "this subscription"
    now = now or dt.datetime.now(dt.UTC)

    calendar = Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", "PUBLISH")

    event = Event()
    event.add("uid", _uid(assessment))
    event.add("dtstamp", now)
    event.add("dtstart", assessment.cancel_by)
    event.add("dtend", assessment.cancel_by + dt.timedelta(days=1))
    event.add("summary", f"Cancel {provider} by today (last safe date)")
    event.add("transp", "TRANSPARENT")

    description_lines = [
        f"Provider: {provider}",
        f"Renewal date: {extraction.renewal_date:%d %b %Y}"
        if extraction.renewal_date
        else "Renewal date: not recorded",
        f"Notice period: {extraction.notice_period_days} days",
        f"Cancel by: {assessment.cancel_by:%d %b %Y}",
    ]
    if extraction.amount is not None:
        currency = f" {extraction.currency}" if extraction.currency else ""
        description_lines.append(f"Amount: {extraction.amount:g}{currency}")
    if extraction.billing_period.value != "unknown":
        description_lines.append(f"Billing period: {extraction.billing_period.value}")
    if extraction.source_quote:
        description_lines += ["", "Source quote:", f'"{extraction.source_quote}"']
    description_lines += [
        "",
        "Renewal Radar is an assistant, not legal advice.",
        "Always verify extracted contract terms against the original document.",
    ]
    event.add("description", "\n".join(description_lines))

    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", f"Cancel {provider} within {lead_days} days")
    alarm.add("trigger", dt.timedelta(days=-abs(lead_days)))
    event.add_component(alarm)

    calendar.add_component(event)
    return calendar.to_ical()
