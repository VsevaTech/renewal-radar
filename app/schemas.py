"""Pydantic models shared by the AI layer, the deterministic core and the web layer."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_NOTICE_PERIOD_DAYS = 3650
MAX_SOURCE_QUOTE_CHARS = 1200


class BillingPeriod(StrEnum):
    """Coarse billing cadence. ``unknown`` is always allowed — we never guess."""

    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"
    BIENNIAL = "biennial"
    OTHER = "other"
    UNKNOWN = "unknown"


class ExtractionStatus(StrEnum):
    """Outcome of combining AI extraction with deterministic date maths."""

    #: Renewal date and notice period are both known — ``cancel_by`` is computed.
    ACTIONABLE = "actionable"
    #: The document says the agreement does not renew automatically.
    NO_AUTO_RENEWAL = "no_auto_renewal"
    #: Something essential is missing or ambiguous. Shown to the user verbatim.
    NEEDS_CONFIRMATION = "needs_confirmation"


NEEDS_CONFIRMATION_LABEL = "Needs confirmation"


class RenewalExtraction(BaseModel):
    """Structured output contract for the AI layer.

    The AI is responsible for *reading* the document only. It never performs date
    arithmetic — ``cancel_by`` is absent from this model by design.
    """

    model_config = ConfigDict(extra="ignore")

    provider: str | None = Field(
        default=None,
        description="Vendor/provider name as written in the document.",
    )
    auto_renewal: bool | None = Field(
        default=None,
        description="True if the agreement renews automatically, false if it does not, "
        "null if the document does not say.",
    )
    renewal_date: dt.date | None = Field(
        default=None,
        description="Next renewal date in YYYY-MM-DD. Null if the document does not state "
        "an unambiguous calendar date.",
    )
    renewal_date_ambiguous: bool = Field(
        default=False,
        description="True when the document implies a renewal date but it cannot be pinned "
        "to a single calendar date.",
    )
    notice_period_days: int | None = Field(
        default=None,
        description="Cancellation notice period converted to whole days. Null if not stated.",
    )
    billing_period: BillingPeriod = Field(
        default=BillingPeriod.UNKNOWN,
        description="Billing cadence stated in the document.",
    )
    amount: float | None = Field(
        default=None, description="Recurring amount, if the document states one."
    )
    currency: str | None = Field(
        default=None, description="ISO-4217 currency code for `amount`, if stated."
    )
    source_quote: str | None = Field(
        default=None,
        description="Verbatim sentence(s) copied from the document that justify the "
        "renewal and notice findings.",
    )
    notes: str | None = Field(
        default=None, description="Short note about anything ambiguous. Optional."
    )

    @field_validator("provider", "currency", "source_quote", "notes", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("currency")
    @classmethod
    def _normalise_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @field_validator("source_quote")
    @classmethod
    def _trim_quote(cls, value: str | None) -> str | None:
        if value and len(value) > MAX_SOURCE_QUOTE_CHARS:
            return value[:MAX_SOURCE_QUOTE_CHARS].rstrip() + "…"
        return value

    @field_validator("notice_period_days")
    @classmethod
    def _sane_notice(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < 0 or value > MAX_NOTICE_PERIOD_DAYS:
            # Out-of-range values are treated as "not stated" rather than trusted.
            return None
        return value

    @field_validator("billing_period", mode="before")
    @classmethod
    def _coerce_billing_period(cls, value: object) -> object:
        if value in (None, ""):
            return BillingPeriod.UNKNOWN
        if isinstance(value, str):
            candidate = value.strip().lower()
            aliases = {
                "yearly": "annual",
                "year": "annual",
                "per year": "annual",
                "annually": "annual",
                "month": "monthly",
                "per month": "monthly",
                "quarter": "quarterly",
                "half-yearly": "semiannual",
                "semi-annual": "semiannual",
                "biannual": "semiannual",
                "two years": "biennial",
            }
            candidate = aliases.get(candidate, candidate)
            if candidate not in {item.value for item in BillingPeriod}:
                return BillingPeriod.OTHER
            return candidate
        return value


class RenewalAssessment(BaseModel):
    """AI findings plus the deterministically computed cancellation deadline."""

    extraction: RenewalExtraction
    status: ExtractionStatus
    cancel_by: dt.date | None = None
    days_until_cancel_by: int | None = None
    reasons: list[str] = Field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return self.status is ExtractionStatus.NEEDS_CONFIRMATION

    @property
    def cancel_by_label(self) -> str:
        if self.cancel_by is None:
            return NEEDS_CONFIRMATION_LABEL
        return self.cancel_by.strftime("%d %b %Y")


class ManualOverride(BaseModel):
    """User corrections applied on top of (or instead of) an AI extraction."""

    model_config = ConfigDict(extra="ignore")

    provider: str | None = None
    auto_renewal: bool | None = None
    renewal_date: dt.date | None = None
    notice_period_days: int | None = None
    billing_period: BillingPeriod = BillingPeriod.UNKNOWN
    amount: float | None = None
    currency: str | None = None
    source_quote: str | None = None

    @field_validator("provider", "currency", "source_quote", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value
