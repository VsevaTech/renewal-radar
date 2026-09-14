"""AI layer behaviour. The Gemini API is mocked — CI never needs a real API key."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import types as pytypes

import pytest
from google.genai import errors as genai_errors

from app.config import DEFAULT_GEMINI_MODEL, Settings
from app.schemas import BillingPeriod, RenewalExtraction
from app.services.ai import (
    AI_UNAVAILABLE_MESSAGE,
    AIError,
    AINotConfiguredError,
    AIQuotaError,
    AIResponseError,
    AITimeoutError,
    AIUnavailableError,
    GeminiRenewalExtractor,
    build_extractor,
    parse_extraction_payload,
)

VALID_PAYLOAD = {
    "provider": "Example SaaS",
    "auto_renewal": True,
    "renewal_date": "2026-11-15",
    "renewal_date_ambiguous": False,
    "notice_period_days": 30,
    "billing_period": "annual",
    "amount": None,
    "currency": None,
    "source_quote": "The agreement renews automatically on 15 November 2026 unless "
    "cancelled at least 30 days before renewal.",
}


class _FakeResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeModels:
    """Stands in for ``client.aio.models``.

    ``script`` is consumed one entry per call: an exception is raised, anything else is
    returned. The last entry repeats once the script runs out, so a single error keeps
    failing every retry.
    """

    def __init__(
        self,
        response=None,
        error: Exception | None = None,
        delay: float = 0.0,
        script: list | None = None,
    ):
        if script is None:
            script = [error if error is not None else response]
        self._script = script
        self._delay = delay
        self.calls = 0
        self.last_kwargs: dict | None = None

    async def generate_content(self, **kwargs):
        self.last_kwargs = kwargs
        step = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if isinstance(step, Exception):
            raise step
        return step


def _extractor_with(models: _FakeModels, **overrides) -> GeminiRenewalExtractor:
    # Zero backoff by default so retry tests do not sleep.
    overrides.setdefault("ai_retry_backoff_seconds", 0.0)
    settings = Settings(gemini_api_key="test-key-not-real", **overrides)
    extractor = GeminiRenewalExtractor.__new__(GeminiRenewalExtractor)
    extractor._settings = settings  # noqa: SLF001 - constructing without a live SDK client
    extractor._client = pytypes.SimpleNamespace(  # noqa: SLF001
        aio=pytypes.SimpleNamespace(models=models)
    )
    return extractor


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


def test_missing_api_key_raises_not_configured():
    with pytest.raises(AINotConfiguredError):
        build_extractor(Settings(gemini_api_key=""))


def test_model_comes_from_environment_not_hardcoded():
    settings = Settings(gemini_api_key="k", gemini_model="gemini-some-other-flash")
    assert settings.resolved_model == "gemini-some-other-flash"


def test_model_falls_back_to_single_default_constant():
    assert Settings(gemini_api_key="k", gemini_model="").resolved_model == DEFAULT_GEMINI_MODEL
    assert Settings(gemini_api_key="k", gemini_model="   ").resolved_model == DEFAULT_GEMINI_MODEL


def test_configured_model_is_passed_to_the_provider():
    models = _FakeModels(response=_FakeResponse(json.dumps(VALID_PAYLOAD)))
    extractor = _extractor_with(models, gemini_model="gemini-test-flash")
    asyncio.run(extractor.extract("some contract text"))
    assert models.last_kwargs is not None
    assert models.last_kwargs["model"] == "gemini-test-flash"


# --------------------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------------------


def test_successful_extraction_is_parsed():
    models = _FakeModels(response=_FakeResponse(json.dumps(VALID_PAYLOAD)))
    result = asyncio.run(_extractor_with(models).extract("contract"))
    assert result.provider == "Example SaaS"
    assert result.auto_renewal is True
    assert result.renewal_date == dt.date(2026, 11, 15)
    assert result.notice_period_days == 30
    assert result.billing_period is BillingPeriod.ANNUAL


def test_document_text_is_truncated_to_the_configured_limit():
    models = _FakeModels(response=_FakeResponse(json.dumps(VALID_PAYLOAD)))
    extractor = _extractor_with(models, max_document_chars=100)
    asyncio.run(extractor.extract("x" * 5000))
    assert models.last_kwargs is not None
    contents = models.last_kwargs["contents"]
    document = contents.split("<document>")[1].split("</document>")[0].strip()
    assert document == "x" * 100


def test_extraction_schema_has_no_cancel_by_field():
    """The AI must never be asked for, or able to supply, the computed deadline."""
    assert "cancel_by" not in RenewalExtraction.model_fields
    assert "cancel_by" not in RenewalExtraction.model_json_schema()["properties"]


# --------------------------------------------------------------------------------------
# malformed responses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not json at all",
        "{",
        '["a", "list"]',
        '"just a string"',
        "null",
    ],
)
def test_malformed_ai_response_raises_ai_response_error(raw):
    with pytest.raises(AIResponseError):
        parse_extraction_payload(raw)


def test_schema_violation_raises_ai_response_error():
    bad = dict(VALID_PAYLOAD, renewal_date="the fifteenth of November")
    with pytest.raises(AIResponseError):
        parse_extraction_payload(json.dumps(bad))


def test_malformed_response_from_provider_surfaces_as_ai_error():
    models = _FakeModels(response=_FakeResponse("<html>gateway error</html>"))
    with pytest.raises(AIResponseError):
        asyncio.run(_extractor_with(models).extract("contract"))


def test_code_fenced_json_is_tolerated():
    fenced = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
    assert parse_extraction_payload(fenced).provider == "Example SaaS"


def test_unknown_extra_fields_are_ignored():
    payload = dict(VALID_PAYLOAD, cancel_by="2026-10-16", confidence=0.9)
    parsed = parse_extraction_payload(json.dumps(payload))
    assert not hasattr(parsed, "cancel_by")


def test_absurd_notice_period_is_treated_as_missing():
    payload = dict(VALID_PAYLOAD, notice_period_days=99999)
    assert parse_extraction_payload(json.dumps(payload)).notice_period_days is None


def test_unknown_billing_period_becomes_other():
    payload = dict(VALID_PAYLOAD, billing_period="every full moon")
    assert parse_extraction_payload(json.dumps(payload)).billing_period is BillingPeriod.OTHER


def test_billing_period_aliases_are_normalised():
    payload = dict(VALID_PAYLOAD, billing_period="Yearly")
    assert parse_extraction_payload(json.dumps(payload)).billing_period is BillingPeriod.ANNUAL


# --------------------------------------------------------------------------------------
# timeouts, quota and other provider errors
# --------------------------------------------------------------------------------------


def test_timeout_raises_ai_timeout_error():
    models = _FakeModels(response=_FakeResponse("{}"), delay=0.5)
    extractor = _extractor_with(models, ai_timeout_seconds=0.01)
    with pytest.raises(AITimeoutError):
        asyncio.run(extractor.extract("contract"))


def test_upstream_gateway_timeout_maps_to_timeout_error():
    error = genai_errors.ClientError(408, {"error": {"message": "Request Timeout"}})
    models = _FakeModels(error=error)
    with pytest.raises(AITimeoutError):
        asyncio.run(_extractor_with(models).extract("contract"))


def test_quota_429_maps_to_quota_error():
    error = genai_errors.ClientError(
        429,
        {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded for model"}},
    )
    models = _FakeModels(error=error)
    with pytest.raises(AIQuotaError):
        asyncio.run(_extractor_with(models).extract("contract"))


def test_rate_limit_wording_maps_to_quota_error():
    error = genai_errors.ClientError(
        400, {"error": {"status": "INVALID_ARGUMENT", "message": "rate limit reached"}}
    )
    models = _FakeModels(error=error)
    with pytest.raises(AIQuotaError):
        asyncio.run(_extractor_with(models).extract("contract"))


def test_server_error_maps_to_transient_unavailable():
    error = genai_errors.ServerError(500, {"error": {"message": "internal"}})
    models = _FakeModels(error=error)
    with pytest.raises(AIUnavailableError) as excinfo:
        asyncio.run(_extractor_with(models).extract("contract"))
    assert not isinstance(excinfo.value, AIQuotaError)


# --------------------------------------------------------------------------------------
# retrying transient failures
# --------------------------------------------------------------------------------------


def _service_unavailable() -> Exception:
    """The 503 Gemini returns when a Flash model is busy."""
    return genai_errors.ServerError(
        503, {"error": {"status": "UNAVAILABLE", "message": "The model is overloaded."}}
    )


def test_503_maps_to_unavailable_and_is_retryable():
    assert AIUnavailableError.retryable is True
    assert AITimeoutError.retryable is True
    assert AIQuotaError.retryable is False
    assert AIResponseError.retryable is False
    assert AIError.retryable is False


def test_transient_503_is_retried_and_then_succeeds():
    models = _FakeModels(
        script=[
            _service_unavailable(),
            _service_unavailable(),
            _FakeResponse(json.dumps(VALID_PAYLOAD)),
        ]
    )
    result = asyncio.run(_extractor_with(models).extract("contract"))
    assert models.calls == 3
    assert result.provider == "Example SaaS"


def test_persistent_503_gives_up_after_the_configured_attempts():
    models = _FakeModels(error=_service_unavailable())
    with pytest.raises(AIUnavailableError):
        asyncio.run(_extractor_with(models, ai_max_attempts=3).extract("contract"))
    assert models.calls == 3


def test_retry_count_is_configurable():
    models = _FakeModels(error=_service_unavailable())
    with pytest.raises(AIUnavailableError):
        asyncio.run(_extractor_with(models, ai_max_attempts=1).extract("contract"))
    assert models.calls == 1


def test_quota_errors_are_never_retried():
    """Retrying a rate-limited free-tier key only burns the remaining allowance."""
    error = genai_errors.ClientError(
        429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
    )
    models = _FakeModels(error=error)
    with pytest.raises(AIQuotaError):
        asyncio.run(_extractor_with(models, ai_max_attempts=5).extract("contract"))
    assert models.calls == 1


def test_bad_request_is_never_retried():
    error = genai_errors.ClientError(400, {"error": {"message": "unsupported model"}})
    models = _FakeModels(error=error)
    with pytest.raises(AIError):
        asyncio.run(_extractor_with(models, ai_max_attempts=5).extract("contract"))
    assert models.calls == 1


def test_retry_backoff_stays_inside_the_overall_timeout():
    """The timeout bounds the whole extraction, retries included."""
    models = _FakeModels(error=_service_unavailable())
    extractor = _extractor_with(
        models, ai_max_attempts=10, ai_retry_backoff_seconds=5.0, ai_timeout_seconds=0.05
    )
    with pytest.raises(AITimeoutError):
        asyncio.run(extractor.extract("contract"))


def test_unexpected_exception_is_wrapped_not_propagated():
    models = _FakeModels(error=RuntimeError("socket exploded"))
    with pytest.raises(AIError):
        asyncio.run(_extractor_with(models).extract("contract"))


def test_every_ai_error_carries_the_required_user_message():
    for error_cls in (AIError, AITimeoutError, AIQuotaError, AIResponseError):
        assert error_cls.user_message == AI_UNAVAILABLE_MESSAGE
    assert "manually" in AINotConfiguredError.user_message


def test_api_key_never_appears_in_error_text():
    secret = "super-secret-key-value"
    error = genai_errors.ClientError(429, {"error": {"message": "Quota exceeded"}})
    models = _FakeModels(error=error)
    extractor = _extractor_with(models)
    extractor._settings = Settings(gemini_api_key=secret)  # noqa: SLF001
    with pytest.raises(AIQuotaError) as excinfo:
        asyncio.run(extractor.extract("contract"))
    assert secret not in str(excinfo.value)
    assert secret not in excinfo.value.user_message
