"""Application settings.

Every AI-provider knob lives here. The model id is read from the ``GEMINI_MODEL``
environment variable and is referenced nowhere else in the codebase, so swapping
models (or providers) never requires touching business logic.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Fallback Flash model used when ``GEMINI_MODEL`` is not set.
#: This is the ONLY place a model id is written down.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"


class Settings(BaseSettings):
    """Runtime configuration, populated from the environment or a local ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_api_key: str = ""
    gemini_model: str = ""

    ai_timeout_seconds: float = 45.0
    max_document_chars: int = 60_000

    database_path: str = "./data/renewal_radar.db"
    app_name: str = "Renewal Radar"

    @property
    def resolved_model(self) -> str:
        """The model id to call, falling back to the built-in default."""
        return self.gemini_model.strip() or DEFAULT_GEMINI_MODEL

    @property
    def ai_configured(self) -> bool:
        """True when an API key is present; the app stays usable when it is not."""
        return bool(self.gemini_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
