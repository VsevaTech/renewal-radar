"""Local SQLite persistence for extraction sessions.

Only the user's own document data lives here. The API key is never stored — it is read
from the environment on every call and never written to disk by this application.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.schemas import RenewalExtraction

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS extractions (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    kind            TEXT NOT NULL,
    document_text   TEXT NOT NULL,
    extraction_json TEXT NOT NULL,
    source          TEXT NOT NULL,
    confirmed       INTEGER NOT NULL DEFAULT 0,
    ai_error        TEXT
);
"""


@dataclass
class StoredExtraction:
    id: str
    created_at: dt.datetime
    updated_at: dt.datetime
    filename: str
    kind: str
    document_text: str
    extraction: RenewalExtraction
    source: str  # "ai" | "manual"
    confirmed: bool
    ai_error: str | None = None


class ExtractionStore:
    """Tiny SQLite-backed store. One row per uploaded/pasted document."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save(
        self,
        *,
        filename: str,
        kind: str,
        document_text: str,
        extraction: RenewalExtraction,
        source: str,
        ai_error: str | None = None,
    ) -> StoredExtraction:
        now = dt.datetime.now(dt.UTC)
        record = StoredExtraction(
            id=uuid.uuid4().hex,
            created_at=now,
            updated_at=now,
            filename=filename,
            kind=kind,
            document_text=document_text,
            extraction=extraction,
            source=source,
            confirmed=False,
            ai_error=ai_error,
        )
        self._conn.execute(
            "INSERT INTO extractions (id, created_at, updated_at, filename, kind, "
            "document_text, extraction_json, source, confirmed, ai_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
                record.filename,
                record.kind,
                record.document_text,
                extraction.model_dump_json(),
                record.source,
                0,
                record.ai_error,
            ),
        )
        self._conn.commit()
        logger.info("Stored extraction %s (%s, %s)", record.id, kind, source)
        return record

    def get(self, extraction_id: str) -> StoredExtraction | None:
        row = self._conn.execute(
            "SELECT * FROM extractions WHERE id = ?", (extraction_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def update(
        self,
        extraction_id: str,
        *,
        extraction: RenewalExtraction,
        source: str,
        confirmed: bool | None = None,
        ai_error: str | None = None,
    ) -> StoredExtraction | None:
        existing = self.get(extraction_id)
        if existing is None:
            return None
        now = dt.datetime.now(dt.UTC)
        self._conn.execute(
            "UPDATE extractions SET updated_at = ?, extraction_json = ?, source = ?, "
            "confirmed = ?, ai_error = ? WHERE id = ?",
            (
                now.isoformat(),
                extraction.model_dump_json(),
                source,
                int(existing.confirmed if confirmed is None else confirmed),
                ai_error,
                extraction_id,
            ),
        )
        self._conn.commit()
        return self.get(extraction_id)

    def delete(self, extraction_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM extractions WHERE id = ?", (extraction_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def recent(self, limit: int = 10) -> list[StoredExtraction]:
        rows = self._conn.execute(
            "SELECT * FROM extractions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> StoredExtraction:
        return StoredExtraction(
            id=row["id"],
            created_at=dt.datetime.fromisoformat(row["created_at"]),
            updated_at=dt.datetime.fromisoformat(row["updated_at"]),
            filename=row["filename"],
            kind=row["kind"],
            document_text=row["document_text"],
            extraction=RenewalExtraction.model_validate(json.loads(row["extraction_json"])),
            source=row["source"],
            confirmed=bool(row["confirmed"]),
            ai_error=row["ai_error"],
        )
