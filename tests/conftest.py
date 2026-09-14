"""Shared fixtures.

The Gemini API is never contacted from the test suite: every test either exercises
pure Python or installs a fake extractor. CI needs no API key.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import BillingPeriod, RenewalExtraction
from app.services.ai import AIError, RenewalExtractor


class FakeExtractor(RenewalExtractor):
    """Returns a canned extraction, or raises a canned error."""

    name = "Fake provider"

    def __init__(
        self,
        result: RenewalExtraction | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def extract(self, document_text: str) -> RenewalExtraction:
        self.calls.append(document_text)
        if self.error is not None:
            raise self.error
        return self.result or RenewalExtraction()


@pytest.fixture
def saas_extraction() -> RenewalExtraction:
    return RenewalExtraction(
        provider="Example SaaS",
        auto_renewal=True,
        renewal_date=dt.date(2026, 11, 15),
        notice_period_days=30,
        billing_period=BillingPeriod.ANNUAL,
        amount=14400.0,
        currency="USD",
        source_quote=(
            "The agreement renews automatically on 15 November 2026 for a further "
            "period of twelve (12) months unless cancelled at least 30 days before renewal."
        ),
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        gemini_api_key="",
        gemini_model="",
        database_path=str(tmp_path / "test.db"),
    )


@pytest.fixture
def make_client(settings):
    """Build a TestClient with a specific fake extractor installed."""

    def _make(extractor: RenewalExtractor | None) -> Iterator[TestClient]:
        app = create_app(settings=settings)
        client = TestClient(app)
        client.__enter__()
        app.state.extractor = extractor  # replaces whatever lifespan built
        return client

    clients: list[TestClient] = []

    def factory(extractor: RenewalExtractor | None = None) -> TestClient:
        client = _make(extractor)
        clients.append(client)
        return client

    yield factory

    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client, saas_extraction) -> TestClient:
    return make_client(FakeExtractor(result=saas_extraction))


@pytest.fixture
def broken_ai_client(make_client) -> TestClient:
    return make_client(FakeExtractor(error=AIError("boom")))
