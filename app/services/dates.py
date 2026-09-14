"""Deterministic renewal maths.

Nothing in this module talks to an AI provider. Given an extraction, the last safe
cancellation date is ``renewal_date - notice_period_days`` — plain Python, fully
reproducible, and covered by unit tests.
"""

from __future__ import annotations

import datetime as dt

from app.schemas import ExtractionStatus, RenewalAssessment, RenewalExtraction


class CancelByError(ValueError):
    """Raised when cancel-by cannot be computed from the inputs given."""


def calculate_cancel_by(renewal_date: dt.date, notice_period_days: int) -> dt.date:
    """Return the last day on which notice can still be given in time.

    ``renewal_date - notice_period_days``. Giving notice on the returned date leaves
    exactly ``notice_period_days`` days before renewal, which satisfies an
    "at least N days before renewal" clause.

    >>> calculate_cancel_by(dt.date(2026, 11, 15), 30)
    datetime.date(2026, 10, 16)
    """
    if not isinstance(renewal_date, dt.date):
        raise CancelByError("renewal_date must be a date")
    if isinstance(renewal_date, dt.datetime):
        renewal_date = renewal_date.date()
    if not isinstance(notice_period_days, int) or isinstance(notice_period_days, bool):
        raise CancelByError("notice_period_days must be an int")
    if notice_period_days < 0:
        raise CancelByError("notice_period_days must not be negative")
    return renewal_date - dt.timedelta(days=notice_period_days)


def assess(extraction: RenewalExtraction, today: dt.date | None = None) -> RenewalAssessment:
    """Combine an extraction with deterministic maths into a user-facing assessment.

    The function never invents a missing date: anything unknown or ambiguous produces
    ``ExtractionStatus.NEEDS_CONFIRMATION`` with an explicit reason.
    """
    today = today or dt.date.today()
    reasons: list[str] = []

    if extraction.auto_renewal is False:
        return RenewalAssessment(
            extraction=extraction,
            status=ExtractionStatus.NO_AUTO_RENEWAL,
            reasons=["The document states the agreement does not renew automatically."],
        )

    if extraction.auto_renewal is None:
        reasons.append("The document does not clearly state whether renewal is automatic.")

    if extraction.renewal_date is None:
        reasons.append("No unambiguous renewal date was found in the document.")
    elif extraction.renewal_date_ambiguous:
        reasons.append("The renewal date in the document is ambiguous.")

    if extraction.notice_period_days is None:
        reasons.append("No cancellation notice period was found in the document.")

    if reasons:
        return RenewalAssessment(
            extraction=extraction,
            status=ExtractionStatus.NEEDS_CONFIRMATION,
            reasons=reasons,
        )

    assert extraction.renewal_date is not None  # noqa: S101 - narrowed above
    assert extraction.notice_period_days is not None  # noqa: S101 - narrowed above

    cancel_by = calculate_cancel_by(extraction.renewal_date, extraction.notice_period_days)
    days_left = (cancel_by - today).days

    if days_left < 0:
        reasons.append("The cancellation deadline has already passed.")
    elif days_left == 0:
        reasons.append("Today is the last day to give notice.")

    return RenewalAssessment(
        extraction=extraction,
        status=ExtractionStatus.ACTIONABLE,
        cancel_by=cancel_by,
        days_until_cancel_by=days_left,
        reasons=reasons,
    )
