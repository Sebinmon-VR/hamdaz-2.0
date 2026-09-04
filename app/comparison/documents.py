"""Turning an uploaded file into something readable.

The output feeds two consumers, and the routing serves both: ``parsing.py``
wants tables as structure so it can read a quote without a model, and
``extraction.py`` wants a form Claude accepts for the documents that defeat it.

* **A PDF with a text layer becomes text and tables.** Most supplier quotes come
  out of an ERP and carry a perfectly good one. Tables are pulled out with their
  rows and columns intact, which is what lets the local parser find a price
  column at all — and, if the model is needed after all, a page of text costs on
  the order of 100 tokens where the same page as an image costs 1,500.

* **A scanned PDF, and every image, goes to the model untouched.** There is no
  text layer to recover and no table to parse, so this is the accurate path and
  the expensive one. ``force_native`` selects it deliberately for a layout the
  text path mangled.

* **Spreadsheets, CSVs and Word files are converted here.** A sheet already *is*
  a table, so it is handed over as one.

Nothing in this module calls the API, and nothing in it interprets a quote. It
decides only what shape a file should take.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Final

#: Anthropic's own request ceiling is 32MB; a supplier quote that size is a
#: mistake rather than a quote, and rejecting it early gives a better error than
#: a 413 from the API.
MAX_FILE_BYTES: Final = 20 * 1_048_576

#: Sent to Claude as a ``document`` block, byte for byte.
PDF_TYPES: Final = frozenset({"application/pdf"})

#: Sent as an ``image`` block. These four are what the API accepts.
IMAGE_TYPES: Final = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

SPREADSHEET_TYPES: Final = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }
)
CSV_TYPES: Final = frozenset({"text/csv", "application/csv"})
WORD_TYPES: Final = frozenset(
    {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
)

#: Browsers are unreliable about MIME types — an .xlsx often arrives as
#: application/octet-stream — so the extension is the fallback authority.
_BY_EXTENSION: Final = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

logger = logging.getLogger("hamdaz.comparison")

#: Below this many characters per page a PDF is a scan with a header on it,
#: not a document with a text layer. Chosen well under a real quote page and
#: well over the stray text an OCR-less scan carries.
_MIN_CHARS_PER_PAGE: Final = 120

#: A converted sheet can run to thousands of rows. Past this the tail is almost
#: always an unrelated price list rather than the quote.
_MAX_TEXT_CHARS: Final = 120_000


class DocumentError(Exception):
    """The file cannot be read. Safe to show a user."""


@dataclass(frozen=True, slots=True)
class Readable:
    """A file, prepared for the model.

    Exactly one of ``data`` (bytes, sent natively) or ``text`` (converted) is
    set. ``kind`` says which content block it becomes.
    """

    kind: str  # "document" | "image" | "text"
    media_type: str
    file_name: str
    data: bytes | None = None
    text: str | None = None
    #: Tables as rows of cells, kept structurally as well as in ``text``.
    #: ``parsing.py`` reads these to pull out line items without a model; the
    #: flattened ``text`` is only for when a model has to look at it instead.
    tables: list[list[list[str]]] = field(default_factory=list)


def resolve_type(file_name: str, declared: str | None) -> str:
    """The file's real type, preferring the extension over what the browser said."""
    suffix = ("." + file_name.rsplit(".", 1)[-1].lower()) if "." in file_name else ""
    if guessed := _BY_EXTENSION.get(suffix):
        return guessed
    if declared and declared != "application/octet-stream":
        return declared.split(";")[0].strip().lower()
    raise DocumentError(
        f"Cannot tell what kind of file {file_name!r} is. "
        f"Accepted: PDF, JPG, PNG, XLSX, CSV, DOCX."
    )


def prepare(
    file_name: str,
    content: bytes,
    declared_type: str | None = None,
    *,
    force_native: bool = False,
) -> Readable:
    """Decide how this file reaches the model, converting only if it must.

    ``force_native`` sends a PDF as a document even when it has a text layer —
    the escape hatch for a quote whose layout defeated the text path.
    """
    if not content:
        raise DocumentError(f"{file_name!r} is empty")
    if len(content) > MAX_FILE_BYTES:
        raise DocumentError(
            f"{file_name!r} is {len(content) // 1_048_576}MB; the limit is "
            f"{MAX_FILE_BYTES // 1_048_576}MB"
        )

    media_type = resolve_type(file_name, declared_type)

    if media_type in PDF_TYPES:
        if not force_native and (extracted := _pdf_text(content)) is not None:
            # Has a real text layer, so the expensive path buys nothing.
            text, tables = extracted
            return Readable("text", media_type, file_name, text=text, tables=tables)
        return Readable("document", media_type, file_name, data=content)
    if media_type in IMAGE_TYPES:
        return Readable("image", media_type, file_name, data=content)
    if media_type in SPREADSHEET_TYPES:
        text, tables = _from_spreadsheet(content, file_name)
        return Readable("text", media_type, file_name, text=text, tables=tables)
    if media_type in CSV_TYPES:
        text, tables = _from_csv(content, file_name)
        return Readable("text", media_type, file_name, text=text, tables=tables)
    if media_type in WORD_TYPES:
        text, tables = _from_word(content, file_name)
        return Readable("text", media_type, file_name, text=text, tables=tables)

    raise DocumentError(
        f"{file_name!r} is a {media_type} file, which this module cannot read. "
        f"Accepted: PDF, JPG, PNG, XLSX, CSV, DOCX."
    )


# ── the cheap path for PDFs ────────────────────────────────────────────


def _pdf_text(content: bytes) -> tuple[str, list[list[list[str]]]] | None:
    """A born-digital PDF as structured text, or ``None`` if it is a scan.

    A page sent as an image costs the model something like 1,500 tokens. The
    same page as text is often nearer 100. Most supplier quotes are generated by
    an ERP and carry a perfectly good text layer, so paying the image price for
    them is pure waste.

    The catch is that naive text extraction destroys tables, and a quote *is* a
    table — so tables are pulled out separately with their rows and columns
    intact and appended as grids. Where that fails, or where there is no text
    layer at all, this returns ``None`` and the caller falls back to sending the
    PDF natively. Accuracy wins that trade; a misread unit price costs more than
    every token this saves.
    """
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - dependency is declared
        return None

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            if not pdf.pages:
                return None

            parts: list[str] = []
            tables: list[list[list[str]]] = []
            characters = 0
            for number, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                characters += len(text)
                page_parts = [text] if text else []

                for table in page.extract_tables() or []:
                    rows = [
                        ["" if cell is None else str(cell) for cell in row]
                        for row in table
                    ]
                    # One-column or one-row "tables" are usually a stray border,
                    # and repeating them only costs tokens.
                    if len(rows) >= 2 and len(rows[0]) >= 2 and (grid := _grid(rows)):
                        page_parts.append(f"[table]\n{grid}")

                if page_parts:
                    parts.append(f"--- page {number} ---\n" + "\n\n".join(page_parts))

            if not parts:
                return None

            # Too little text for the page count means a scan with, at most, a
            # header stamped on it. Send the real thing and let vision read it.
            if characters / len(pdf.pages) < _MIN_CHARS_PER_PAGE:
                return None

            return "\n\n".join(parts)[:_MAX_TEXT_CHARS], tables
    except Exception:  # noqa: BLE001 - any failure just means "send the PDF"
        logger.info("no usable text layer; sending the PDF natively", exc_info=True)
        return None


# ── conversions ────────────────────────────────────────────────────────


def _grid(rows: list[list[str]]) -> str:
    """Rows as a pipe-delimited table, blank rows dropped.

    Deliberately not JSON or prose: a grid keeps each price under its own
    heading, which is the one structural fact the model needs to read a quote
    correctly.
    """
    lines = [
        " | ".join(cell.strip() for cell in row)
        for row in rows
        if any(cell and cell.strip() for cell in row)
    ]
    return "\n".join(lines)[:_MAX_TEXT_CHARS]


def _from_spreadsheet(
    content: bytes, file_name: str
) -> tuple[str, list[list[list[str]]]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentError("Spreadsheet support is not installed") from exc

    try:
        # read_only streams rather than building the whole tree; data_only takes
        # the cached result of a formula, since "=B2*C2" tells us nothing.
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many shapes
        raise DocumentError(f"{file_name!r} is not a readable spreadsheet: {exc}") from exc

    parts: list[str] = []
    tables: list[list[list[str]]] = []
    try:
        for sheet in workbook.worksheets:
            rows = [
                ["" if cell is None else str(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            populated = [r for r in rows if any(c and c.strip() for c in r)]
            if grid := _grid(rows):
                # A sheet already *is* a table, so it is offered to the local
                # parser as one rather than only as flattened text.
                if len(populated) >= 2:
                    tables.append(populated)
                # Quotes often put the commercial terms on a second sheet.
                parts.append(f"--- sheet: {sheet.title} ---\n{grid}")
    finally:
        workbook.close()

    if not parts:
        raise DocumentError(f"{file_name!r} has no readable cells")
    return "\n\n".join(parts)[:_MAX_TEXT_CHARS], tables


def _from_csv(content: bytes, file_name: str) -> tuple[str, list[list[list[str]]]]:
    text = _decode(content, file_name)
    try:
        # Supplier exports are as often semicolon- or tab-delimited as comma.
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text), dialect)
        if any(cell and cell.strip() for cell in row)
    ]
    grid = _grid(rows)
    if not grid:
        raise DocumentError(f"{file_name!r} has no readable rows")
    return grid, ([rows] if len(rows) >= 2 else [])


def _from_word(content: bytes, file_name: str) -> tuple[str, list[list[list[str]]]]:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DocumentError("Word support is not installed") from exc

    try:
        document = docx.Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001 - python-docx raises many shapes
        raise DocumentError(f"{file_name!r} is not a readable Word file: {exc}") from exc

    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    tables: list[list[list[str]]] = []
    # Tables carry the prices and are not in `paragraphs`; without this a Word
    # quote arrives as a covering letter with no numbers in it.
    for table in document.tables:
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if grid := _grid(rows):
            if len(rows) >= 2 and len(rows[0]) >= 2:
                tables.append(rows)
            parts.append(grid)

    if not parts:
        raise DocumentError(f"{file_name!r} has no readable text")
    return "\n\n".join(parts)[:_MAX_TEXT_CHARS], tables


def _decode(content: bytes, file_name: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentError(f"Cannot decode {file_name!r} as text")
