"""AI provider layer.

Everything model- and vendor-specific lives behind :class:`RenewalExtractor`. The rest
of the application depends only on the abstract interface and on
:class:`~app.schemas.RenewalExtraction`, so swapping Gemini for another provider means
adding one class here and nothing else.

Hard rules enforced by this module:

* the AI never computes ``cancel_by`` — that is deterministic Python
  (see :mod:`app.services.dates`);
* the AI never invents a renewal date it cannot quote;
* document text is never written to the logs;
* the API key is never written to the logs, the database, or an error message.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas import RenewalExtraction

logger = logging.getLogger(__name__)

#: Shown verbatim in the UI whenever extraction cannot be completed.
AI_UNAVAILABLE_MESSAGE = (
    "AI extraction is temporarily unavailable.\n"
    "You can retry later or enter renewal details manually."
)

SYSTEM_INSTRUCTION = """\
You are a contract-reading assistant for Renewal Radar. You read one document and
report only what it actually says about automatic renewal.

Absolute rules:
1. NEVER invent, infer, estimate or "helpfully" complete a renewal date. If the document
   does not state a specific calendar date for the next renewal, set "renewal_date" to
   null. A date you cannot quote from the document does not exist.
2. If the document implies a renewal date but it cannot be resolved to a single calendar
   date (for example "renews on the anniversary of the effective date" with no effective
   date given, or two conflicting dates), set "renewal_date" to null AND
   "renewal_date_ambiguous" to true.
3. NEVER compute a cancellation deadline. Do not subtract the notice period from
   anything. Report the notice period only.
4. "source_quote" must be copied verbatim from the document — a contiguous span of the
   original text that contains the renewal and/or notice language. Do not paraphrase.
   If you cannot find such a span, set it to null.
5. Convert the notice period to whole days: "30 days" -> 30, "one month" -> 30,
   "two months" -> 60, "90 days" -> 90, "6 weeks" -> 42. If no notice period is stated,
   set "notice_period_days" to null. Do not assume a default notice period.
6. "auto_renewal" is true only when the document says renewal happens without action by
   the customer. Set it to false when the document says the agreement ends or must be
   renewed explicitly. Set it to null when the document is silent.
7. Report "amount" and "currency" only when the document states a recurring charge.
   Otherwise null.

Output JSON matching the provided schema. No commentary, no markdown fences.\
"""

USER_PROMPT_TEMPLATE = """\
Extract the renewal terms from the document below.

<document>
{document_text}
</document>
"""


class AIError(RuntimeError):
    """Base class for every recoverable AI failure.

    The web layer turns any :class:`AIError` into :data:`AI_UNAVAILABLE_MESSAGE` plus a
    manual-entry form. The application must never crash because of one.
    """

    user_message = AI_UNAVAILABLE_MESSAGE


class AINotConfiguredError(AIError):
    """No API key is configured, so the AI path is unavailable by design."""

    user_message = (
        "AI extraction is not configured on this instance.\n"
        "You can enter the renewal details manually."
    )


class AITimeoutError(AIError):
    """The provider did not answer within the configured timeout."""


class AIQuotaError(AIError):
    """The provider rejected the call with a quota or rate-limit error."""

    user_message = (
        "AI extraction is temporarily unavailable.\n"
        "You can retry later or enter renewal details manually."
    )


class AIResponseError(AIError):
    """The provider answered, but not with something we can parse."""


class RenewalExtractor(ABC):
    """Provider-agnostic interface used by the rest of the application."""

    #: Human-readable provider label, shown in the UI footer.
    name: str = "unknown"

    @abstractmethod
    async def extract(self, document_text: str) -> RenewalExtraction:
        """Return structured renewal terms for ``document_text``.

        Implementations must raise a subclass of :class:`AIError` on any failure and
        must never let a provider-specific exception escape.
        """


def _strip_code_fence(raw: str) -> str:
    """Tolerate a model that wraps JSON in a markdown fence."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    return text.strip()


def parse_extraction_payload(raw_text: str | None) -> RenewalExtraction:
    """Validate a raw provider response into :class:`RenewalExtraction`.

    Kept separate from transport so malformed-response handling is unit-testable
    without touching the network.
    """
    if not raw_text or not raw_text.strip():
        raise AIResponseError("The AI provider returned an empty response.")

    try:
        payload: Any = json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError as exc:
        raise AIResponseError("The AI provider returned malformed JSON.") from exc

    if not isinstance(payload, dict):
        raise AIResponseError("The AI provider returned JSON that is not an object.")

    try:
        return RenewalExtraction.model_validate(payload)
    except ValidationError as exc:
        raise AIResponseError("The AI response did not match the expected schema.") from exc


class GeminiRenewalExtractor(RenewalExtractor):
    """Google Gemini implementation, using the official ``google-genai`` SDK.

    The model id comes from ``GEMINI_MODEL`` via :class:`~app.config.Settings` and is not
    hardcoded here.
    """

    name = "Google Gemini"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.ai_configured:
            raise AINotConfiguredError("GEMINI_API_KEY is not set.")
        self._client = self._build_client()

    @property
    def model(self) -> str:
        return self._settings.resolved_model

    def _build_client(self) -> Any:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise AIError("The Google Gen AI SDK is not installed.") from exc

        return genai.Client(
            api_key=self._settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=int(self._settings.ai_timeout_seconds * 1000),
            ),
        )

    def _config(self) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=RenewalExtraction,
            temperature=0.0,
        )

    async def extract(self, document_text: str) -> RenewalExtraction:
        from google.genai import errors as genai_errors

        prompt = USER_PROMPT_TEMPLATE.format(
            document_text=document_text[: self._settings.max_document_chars]
        )

        try:
            response = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=self._config(),
                ),
                timeout=self._settings.ai_timeout_seconds,
            )
        except TimeoutError as exc:
            # Never log `prompt` — it contains the user's document.
            logger.warning("Gemini call timed out after %ss", self._settings.ai_timeout_seconds)
            raise AITimeoutError("The AI provider timed out.") from exc
        except genai_errors.APIError as exc:
            raise _map_api_error(exc) from exc
        except AIError:
            raise
        except Exception as exc:  # noqa: BLE001 - the app must not crash on provider bugs
            logger.warning("Gemini call failed: %s", type(exc).__name__)
            raise AIError("The AI provider call failed.") from exc

        return parse_extraction_payload(getattr(response, "text", None))


_QUOTA_STATUS_CODES = {429}
_QUOTA_KEYWORDS = ("quota", "rate limit", "rate_limit", "resource_exhausted", "exhausted")


def _map_api_error(exc: Exception) -> AIError:
    """Translate a provider error into our own taxonomy, without leaking the key."""
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "")
    message = str(getattr(exc, "message", "") or "")
    haystack = f"{status} {message}".lower()

    if code in _QUOTA_STATUS_CODES or any(word in haystack for word in _QUOTA_KEYWORDS):
        logger.warning("Gemini quota/rate limit hit (code=%s)", code)
        return AIQuotaError("The AI provider quota is exhausted.")

    if code in {408, 504}:
        logger.warning("Gemini upstream timeout (code=%s)", code)
        return AITimeoutError("The AI provider timed out.")

    logger.warning("Gemini API error (code=%s)", code)
    return AIError("The AI provider returned an error.")


def build_extractor(settings: Settings | None = None) -> RenewalExtractor:
    """Factory for the configured provider.

    Raises :class:`AINotConfiguredError` when no key is present; callers fall back to
    the manual-entry flow.
    """
    settings = settings or get_settings()
    return GeminiRenewalExtractor(settings)
