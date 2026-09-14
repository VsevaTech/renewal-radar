"""Orchestration between document parsing, the AI layer and the deterministic core."""

from __future__ import annotations

import logging

from app.schemas import ManualOverride, RenewalExtraction

logger = logging.getLogger(__name__)


def apply_override(base: RenewalExtraction | None, override: ManualOverride) -> RenewalExtraction:
    """Merge user corrections onto an extraction.

    A field the user left blank keeps the AI's value; a field the user filled in wins.
    Manually supplying a renewal date always clears the ``ambiguous`` flag, because the
    human has just resolved it.
    """
    base = base or RenewalExtraction()

    renewal_date = override.renewal_date if override.renewal_date is not None else base.renewal_date
    ambiguous = base.renewal_date_ambiguous
    if override.renewal_date is not None:
        ambiguous = False

    auto_renewal = base.auto_renewal if override.auto_renewal is None else override.auto_renewal

    billing_period = base.billing_period
    if override.billing_period.value != "unknown":
        billing_period = override.billing_period

    notice = (
        override.notice_period_days
        if override.notice_period_days is not None
        else base.notice_period_days
    )

    return RenewalExtraction(
        provider=override.provider or base.provider,
        auto_renewal=auto_renewal,
        renewal_date=renewal_date,
        renewal_date_ambiguous=ambiguous,
        notice_period_days=notice,
        billing_period=billing_period,
        amount=override.amount if override.amount is not None else base.amount,
        currency=override.currency or base.currency,
        source_quote=override.source_quote or base.source_quote,
        notes=base.notes,
    )
