#!/usr/bin/env python3
"""Manual live check against the real Gemini API.

Not part of the test suite and not run in CI — the automated tests mock the provider
entirely. Use this once, locally, to confirm your free-tier key and model id work:

    GEMINI_API_KEY=... GEMINI_MODEL=gemini-3.5-flash python scripts/live_check.py

With no GEMINI_MODEL set it lists the Flash models your key can actually see, so you
can copy one into your .env.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.services.ai import AIError, GeminiRenewalExtractor  # noqa: E402
from app.services.dates import assess  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "saas-contract.txt"


def list_flash_models(settings: Settings) -> None:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    print("Flash models visible to this key:")
    for model in client.models.list():
        name = model.name or ""
        if "flash" in name and "generateContent" in (model.supported_actions or []):
            print(f"  {name.removeprefix('models/')}")


async def main() -> int:
    settings = Settings()
    if not settings.ai_configured:
        print("Set GEMINI_API_KEY first. Get a free key at https://aistudio.google.com/apikey")
        return 2

    if not os.environ.get("GEMINI_MODEL"):
        list_flash_models(settings)
        print(f"\nNo GEMINI_MODEL set — trying the built-in default: {settings.resolved_model}\n")

    print(f"Model: {settings.resolved_model}")
    try:
        extraction = await GeminiRenewalExtractor(settings).extract(SAMPLE.read_text())
    except AIError as exc:
        print(f"\nFAILED: {type(exc).__name__}")
        print(exc.user_message)
        return 1

    result = assess(extraction)
    print(f"\nProvider:      {extraction.provider}")
    print(f"Auto-renewal:  {extraction.auto_renewal}")
    print(f"Renewal date:  {extraction.renewal_date}")
    print(f"Notice period: {extraction.notice_period_days} days")
    print(f"Billing:       {extraction.billing_period.value}")
    print(f"Amount:        {extraction.amount} {extraction.currency or ''}")
    print(f"Source quote:  {extraction.source_quote}")
    print(f"\nCancel by:     {result.cancel_by_label}   <- computed in Python, not by the AI")

    expected = "16 Oct 2026"
    ok = result.cancel_by_label == expected
    print(f"\n{'PASS' if ok else 'MISMATCH'}: expected {expected}, got {result.cancel_by_label}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
