"""The deterministic core: cancel_by = renewal_date - notice_period_days."""

from __future__ import annotations

import datetime as dt

import pytest

from app.schemas import BillingPeriod, ExtractionStatus, RenewalExtraction
from app.services.dates import CancelByError, assess, calculate_cancel_by


def test_readme_example_fixed_renewal_date_30_day_notice():
    """The worked example from the README and the product spec."""
    assert calculate_cancel_by(dt.date(2026, 11, 15), 30) == dt.date(2026, 10, 16)


def test_cancel_by_leaves_exactly_the_notice_period():
    renewal = dt.date(2026, 11, 15)
    cancel_by = calculate_cancel_by(renewal, 30)
    assert (renewal - cancel_by).days == 30


def test_60_day_notice():
    assert calculate_cancel_by(dt.date(2027, 3, 1), 60) == dt.date(2026, 12, 31)


@pytest.mark.parametrize(
    ("renewal", "notice", "expected"),
    [
        (dt.date(2026, 1, 1), 30, dt.date(2025, 12, 2)),  # crosses a year boundary
        (dt.date(2026, 3, 1), 1, dt.date(2026, 2, 28)),  # non-leap February
        (dt.date(2028, 3, 1), 1, dt.date(2028, 2, 29)),  # leap day exists
        (dt.date(2028, 3, 1), 60, dt.date(2028, 1, 1)),  # 60 days across a leap February
        (dt.date(2027, 3, 1), 60, dt.date(2026, 12, 31)),  # same span, non-leap year
        (dt.date(2028, 2, 29), 365, dt.date(2027, 3, 1)),  # leap day as the renewal date
        (dt.date(2026, 11, 15), 0, dt.date(2026, 11, 15)),  # zero notice = renewal day itself
        (dt.date(2026, 6, 30), 90, dt.date(2026, 4, 1)),  # 90-day notice
    ],
)
def test_date_arithmetic_including_leap_years(renewal, notice, expected):
    assert calculate_cancel_by(renewal, notice) == expected


def test_leap_year_span_differs_from_common_year_span():
    """A 60-day notice lands on a different month-day depending on February's length."""
    leap = calculate_cancel_by(dt.date(2028, 3, 1), 60)
    common = calculate_cancel_by(dt.date(2027, 3, 1), 60)
    assert leap == dt.date(2028, 1, 1)
    assert common == dt.date(2026, 12, 31)


def test_negative_notice_period_is_rejected():
    with pytest.raises(CancelByError):
        calculate_cancel_by(dt.date(2026, 11, 15), -1)


def test_non_integer_notice_period_is_rejected():
    with pytest.raises(CancelByError):
        calculate_cancel_by(dt.date(2026, 11, 15), 30.5)  # type: ignore[arg-type]


def test_bool_is_not_accepted_as_notice_period():
    with pytest.raises(CancelByError):
        calculate_cancel_by(dt.date(2026, 11, 15), True)  # type: ignore[arg-type]


def test_non_date_renewal_is_rejected():
    with pytest.raises(CancelByError):
        calculate_cancel_by("2026-11-15", 30)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# assess(): status resolution
# --------------------------------------------------------------------------------------


def test_annual_renewal_is_actionable(saas_extraction):
    result = assess(saas_extraction, today=dt.date(2026, 9, 14))
    assert result.status is ExtractionStatus.ACTIONABLE
    assert result.extraction.billing_period is BillingPeriod.ANNUAL
    assert result.cancel_by == dt.date(2026, 10, 16)
    assert result.days_until_cancel_by == 32
    assert result.cancel_by_label == "16 Oct 2026"


def test_no_auto_renewal_has_no_deadline():
    extraction = RenewalExtraction(
        provider="Meridian Facilities Services LLC",
        auto_renewal=False,
        source_quote="this Agreement shall terminate and shall not renew automatically.",
    )
    result = assess(extraction)
    assert result.status is ExtractionStatus.NO_AUTO_RENEWAL
    assert result.cancel_by is None
    assert "does not renew automatically" in result.reasons[0]


def test_missing_renewal_date_needs_confirmation():
    extraction = RenewalExtraction(auto_renewal=True, notice_period_days=30)
    result = assess(extraction)
    assert result.status is ExtractionStatus.NEEDS_CONFIRMATION
    assert result.cancel_by is None
    assert result.cancel_by_label == "Needs confirmation"
    assert any("No unambiguous renewal date" in reason for reason in result.reasons)


def test_ambiguous_renewal_date_needs_confirmation():
    """A date the model could not pin down must never be turned into a deadline."""
    extraction = RenewalExtraction(
        auto_renewal=True,
        renewal_date=dt.date(2026, 11, 15),
        renewal_date_ambiguous=True,
        notice_period_days=30,
    )
    result = assess(extraction)
    assert result.status is ExtractionStatus.NEEDS_CONFIRMATION
    assert result.cancel_by is None
    assert any("ambiguous" in reason for reason in result.reasons)


def test_missing_notice_period_needs_confirmation():
    extraction = RenewalExtraction(auto_renewal=True, renewal_date=dt.date(2026, 11, 15))
    result = assess(extraction)
    assert result.status is ExtractionStatus.NEEDS_CONFIRMATION
    assert any("notice period" in reason for reason in result.reasons)


def test_unknown_auto_renewal_needs_confirmation():
    extraction = RenewalExtraction(renewal_date=dt.date(2026, 11, 15), notice_period_days=30)
    result = assess(extraction)
    assert result.status is ExtractionStatus.NEEDS_CONFIRMATION


def test_empty_extraction_needs_confirmation():
    result = assess(RenewalExtraction())
    assert result.status is ExtractionStatus.NEEDS_CONFIRMATION
    assert len(result.reasons) == 3


def test_past_deadline_is_flagged_but_still_computed(saas_extraction):
    result = assess(saas_extraction, today=dt.date(2026, 12, 1))
    assert result.status is ExtractionStatus.ACTIONABLE
    assert result.cancel_by == dt.date(2026, 10, 16)
    assert result.days_until_cancel_by == -46
    assert any("already passed" in reason for reason in result.reasons)


def test_deadline_today_is_flagged(saas_extraction):
    result = assess(saas_extraction, today=dt.date(2026, 10, 16))
    assert result.days_until_cancel_by == 0
    assert any("last day" in reason for reason in result.reasons)
