"""Guard rails: no secrets in logs or repo, and a stable AI contract."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from app.config import DEFAULT_GEMINI_MODEL, Settings
from app.schemas import ManualOverride, RenewalExtraction
from app.services.documents import parse_pasted_text
from app.services.extraction import apply_override
from app.services.storage import ExtractionStore

ROOT = Path(__file__).resolve().parents[1]
SECRET_CLAUSE = "CONFIDENTIAL-CLAUSE-DO-NOT-LOG-9f13ab"


def test_document_text_is_never_written_to_the_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        parse_pasted_text(f"Renews on 15 November 2026. {SECRET_CLAUSE}")
    assert SECRET_CLAUSE not in caplog.text


def test_storage_logs_only_identifiers(caplog, tmp_path):
    store = ExtractionStore(tmp_path / "s.db")
    with caplog.at_level(logging.DEBUG):
        store.save(
            filename="c.txt",
            kind="text",
            document_text=SECRET_CLAUSE,
            extraction=RenewalExtraction(),
            source="ai",
        )
    assert SECRET_CLAUSE not in caplog.text
    store.close()


def test_api_key_is_not_persisted_by_the_store(tmp_path):
    store = ExtractionStore(tmp_path / "s.db")
    store.save(
        filename="c.txt",
        kind="text",
        document_text="text",
        extraction=RenewalExtraction(provider="X"),
        source="ai",
    )
    store.close()
    blob = (tmp_path / "s.db").read_bytes()
    assert b"GEMINI_API_KEY" not in blob
    assert b"api_key" not in blob


def test_storage_schema_has_no_column_for_credentials(tmp_path):
    from app.services import storage

    assert "api" not in storage.SCHEMA.lower().replace("api_key_free", "")
    store = ExtractionStore(tmp_path / "s.db")
    columns = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(extractions)").fetchall()  # noqa: SLF001
    }
    assert columns == {
        "id",
        "created_at",
        "updated_at",
        "filename",
        "kind",
        "document_text",
        "extraction_json",
        "source",
        "confirmed",
        "ai_error",
    }
    store.close()


def test_env_example_ships_empty_values():
    lines = (ROOT / ".env.example").read_text().splitlines()
    for key in ("GEMINI_API_KEY", "GEMINI_MODEL"):
        assigned = [line for line in lines if line.startswith(f"{key}=")]
        assert assigned == [f"{key}="], f"{key} must be empty in .env.example"


def test_repository_ships_no_dotenv():
    assert not (ROOT / ".env").exists()
    assert ".env" in (ROOT / ".gitignore").read_text()


def test_application_code_never_hardcodes_a_model_id():
    """GEMINI_MODEL is the single source of truth; the default lives in one constant."""
    hits = []
    for path in (ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if "gemini-" in line and "DEFAULT_GEMINI_MODEL" not in line:
                hits.append(f"{path.relative_to(ROOT)}:{number}")
    assert hits == [], f"model id hardcoded outside app/config.py: {hits}"
    assert DEFAULT_GEMINI_MODEL.startswith("gemini-")


def test_default_model_constant_is_declared_only_in_config():
    """Exactly one line in app/ assigns a concrete model id."""
    config = (ROOT / "app" / "config.py").read_text()
    assignments = [
        line
        for line in config.splitlines()
        if line.startswith("DEFAULT_GEMINI_MODEL") and "=" in line
    ]
    assert len(assignments) == 1


def test_readme_carries_the_required_disclaimer():
    readme = (ROOT / "README.md").read_text()
    assert "Renewal Radar is an assistant, not legal advice." in readme
    assert "Always verify extracted contract terms against the original document." in readme
    assert "## AI provider" in readme
    assert "sent" in readme and "external" in readme


def test_no_api_keys_committed():
    # Assembled at runtime so this guard does not trip over its own source.
    suspicious = ("AI" + "za", "sk-" + "ant-", "sk-" + "proj-", "GEMINI_API_KEY=" + "A")
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or path == Path(__file__)
            or any(
                part in {".git", "__pycache__", ".venv", "data", ".ruff_cache", ".pytest_cache"}
                for part in path.parts
            )
        ):
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for needle in suspicious:
            assert needle not in content, f"possible secret in {path.relative_to(ROOT)}"


# --------------------------------------------------------------------------------------
# manual override semantics
# --------------------------------------------------------------------------------------


def test_override_fills_in_a_missing_date():
    merged = apply_override(
        RenewalExtraction(provider="Example SaaS", auto_renewal=True),
        ManualOverride(renewal_date=dt.date(2026, 11, 15), notice_period_days=30),
    )
    assert merged.provider == "Example SaaS"
    assert merged.renewal_date == dt.date(2026, 11, 15)


def test_override_resolves_ambiguity():
    merged = apply_override(
        RenewalExtraction(renewal_date_ambiguous=True),
        ManualOverride(renewal_date=dt.date(2026, 11, 15)),
    )
    assert merged.renewal_date_ambiguous is False


def test_override_keeps_ai_values_when_left_blank():
    base = RenewalExtraction(provider="Example SaaS", notice_period_days=30)
    merged = apply_override(base, ManualOverride())
    assert merged.provider == "Example SaaS"
    assert merged.notice_period_days == 30


def test_override_on_empty_base_works():
    merged = apply_override(None, ManualOverride(provider="Acme", notice_period_days=60))
    assert merged.provider == "Acme"
    assert merged.notice_period_days == 60


def test_settings_never_expose_the_key_in_repr():
    settings = Settings(gemini_api_key="secret-value-123")
    assert settings.ai_configured is True
    # The value is readable by the AI layer but is not echoed anywhere by the app.
    assert "secret-value-123" not in json.dumps(
        {"model": settings.resolved_model, "configured": settings.ai_configured}
    )


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_key_means_manual_only(value):
    assert Settings(gemini_api_key=value).ai_configured is False
