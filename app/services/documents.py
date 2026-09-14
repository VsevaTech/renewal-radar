"""Turn an uploaded file into plain text.

Supported inputs: PDF, DOCX, and plain text (including pasted text). Nothing here calls
out to a network service — extraction is local.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB

PDF_TYPES = {"application/pdf", "application/x-pdf"}
DOCX_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
TEXT_TYPES = {"text/plain", "text/markdown", "application/octet-stream", ""}

_WHITESPACE_RUN = re.compile("[ \\t\\u00a0]+")  # space, tab, non-breaking space
_BLANK_LINES = re.compile(r"\n{3,}")


class DocumentError(ValueError):
    """Raised when a document cannot be read. Always safe to show to the user."""


@dataclass(frozen=True)
class ParsedDocument:
    """Extracted text plus the little metadata the UI needs."""

    text: str
    filename: str
    kind: str  # "pdf" | "docx" | "text"

    @property
    def char_count(self) -> int:
        return len(self.text)


def normalise_text(raw: str) -> str:
    """Collapse the whitespace noise that PDF extraction tends to produce."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RUN.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", text).strip()


def _detect_kind(filename: str, content_type: str | None, data: bytes) -> str:
    lowered = (filename or "").lower()
    ctype = (content_type or "").split(";")[0].strip().lower()

    if data[:5] == b"%PDF-" or ctype in PDF_TYPES or lowered.endswith(".pdf"):
        return "pdf"
    # DOCX is a zip; the magic bytes alone are not enough, so pair them with the name/type.
    if ctype in DOCX_TYPES or lowered.endswith(".docx"):
        return "docx"
    if lowered.endswith((".txt", ".text", ".md")) or ctype in TEXT_TYPES:
        return "text"
    raise DocumentError("Unsupported file type. Renewal Radar accepts PDF, DOCX and plain text.")


def extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentError("PDF support is not installed.") from exc

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise DocumentError("This PDF is password protected.") from exc
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentError:
        raise
    except PdfReadError as exc:
        raise DocumentError("This file could not be read as a PDF.") from exc
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide range of errors
        raise DocumentError("This file could not be read as a PDF.") from exc

    return "\n\n".join(pages)


def extract_docx_text(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentError("DOCX support is not installed.") from exc

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises PackageNotFoundError etc.
        raise DocumentError("This file could not be read as a DOCX document.") from exc

    chunks = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                chunks.append(" | ".join(cells))
    return "\n".join(chunks)


def extract_plain_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise DocumentError("This file is not readable as text.")


def parse_document(
    data: bytes, filename: str = "", content_type: str | None = None
) -> ParsedDocument:
    """Read ``data`` into :class:`ParsedDocument`, choosing a parser by type.

    Raises :class:`DocumentError` — never an unhandled exception — for anything the app
    cannot read.
    """
    if not data:
        raise DocumentError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError("The uploaded file is larger than 10 MB.")

    kind = _detect_kind(filename, content_type, data)
    extractor = {
        "pdf": extract_pdf_text,
        "docx": extract_docx_text,
        "text": extract_plain_text,
    }[kind]

    text = normalise_text(extractor(data))
    if not text:
        raise DocumentError(
            "No text could be extracted from this file. "
            "If it is a scanned document, paste the relevant text instead."
        )
    # Deliberately log the size only — never the content.
    logger.info("Parsed %s document (%d characters)", kind, len(text))
    return ParsedDocument(text=text, filename=filename or f"pasted.{kind}", kind=kind)


def parse_pasted_text(raw: str) -> ParsedDocument:
    text = normalise_text(raw or "")
    if not text:
        raise DocumentError("Paste some contract text first.")
    logger.info("Parsed pasted text (%d characters)", len(text))
    return ParsedDocument(text=text, filename="pasted-text.txt", kind="text")
