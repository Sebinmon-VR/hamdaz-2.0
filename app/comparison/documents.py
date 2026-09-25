"""Turning an uploaded file into something readable.

The output feeds ``parsing.py``, which reads a supplier quote with no model
behind it, so what matters here is *structure*: a quote is a table, and the
parser needs each price under its own heading.

* **A PDF with a text layer becomes text and tables.** Most supplier quotes come
  out of an ERP and carry a perfectly good one. Where the PDF draws its table
  with ruling lines, pdfplumber finds it as a grid. Where it does not — a web
  shop's printed order page, a quote typed in Word — the grid is rebuilt from
  the positions of the words on the page: the column headings say where each
  column is, and every word below them falls into the column it sits under.

* **A scanned PDF, and every image, has no text to read.** An OCR engine can
  be plugged in (``rapidocr_onnxruntime``, if installed) and its boxes go
  through the same positional builder; without one the document is handed
  back as ``kind="document"`` and the extractor says plainly that it must be
  typed in.

* **Spreadsheets, CSVs and Word files are converted here.** A sheet already *is*
  a table, so it is handed over as one.

Nothing in this module interprets a quote. It decides only what shape a file
should take.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Final

#: A supplier quote bigger than this is a mistake rather than a quote, and
#: refusing it early gives a better error than running out of memory on it.
MAX_FILE_BYTES: Final = 20 * 1_048_576

PDF_TYPES: Final = frozenset({"application/pdf"})

#: Photographs and screenshots of a quote. Readable only through OCR.
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
    """A file, prepared for reading.

    ``kind`` is ``"text"`` when there is something to parse — ``text`` and
    ``tables`` are then set — and ``"document"`` or ``"image"`` when there is
    not: a scan or a photograph, carried as bytes for an OCR engine if one is
    installed, and otherwise declined by the extractor.
    """

    kind: str  # "document" | "image" | "text"
    media_type: str
    file_name: str
    data: bytes | None = None
    text: str | None = None
    #: Tables as rows of cells, kept structurally as well as in ``text``.
    #: ``parsing.py`` reads these to pull out line items; the flattened
    #: ``text`` carries the labelled fields around the table.
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
    """Decide what shape this file takes, converting only if it must.

    ``force_native`` treats a PDF as a scan even when it has a text layer —
    the escape hatch for a quote whose text layer is garbage (some print
    drivers emit one glyph per word), so it goes through OCR instead.
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
            text, tables = extracted
            return Readable("text", media_type, file_name, text=text, tables=tables)
        # No text layer. OCR if an engine is installed; otherwise the bytes are
        # carried so the extractor can say what this is and what to do instead.
        if (read := _ocr(content, media_type)) is not None:
            text, tables = read
            return Readable("text", media_type, file_name, text=text, tables=tables)
        return Readable("document", media_type, file_name, data=content)
    if media_type in IMAGE_TYPES:
        if (read := _ocr(content, media_type)) is not None:
            text, tables = read
            return Readable("text", media_type, file_name, text=text, tables=tables)
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


# ── PDFs with a text layer ─────────────────────────────────────────────


def _pdf_text(content: bytes) -> tuple[str, list[list[list[str]]]] | None:
    """A born-digital PDF as structured text, or ``None`` if it is a scan.

    Naive text extraction destroys tables, and a quote *is* a table — so tables
    are pulled out separately with their rows and columns intact. A PDF that
    rules its table gives pdfplumber a grid to find. One that does not — most
    web-shop order pages, anything printed from Word — gets its grid rebuilt
    from where the words sit on the page (see :func:`table_from_words`).

    Where there is no text layer at all this returns ``None`` and the caller
    tries OCR, or declines.
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

                found = False
                for table in page.extract_tables() or []:
                    rows = [
                        ["" if cell is None else str(cell) for cell in row]
                        for row in table
                    ]
                    # One-column or one-row "tables" are usually a stray border.
                    if len(rows) >= 2 and len(rows[0]) >= 2 and (grid := _grid(rows)):
                        page_parts.append(f"[table]\n{grid}")
                        tables.append(rows)
                        found = True

                if not found:
                    words = [
                        Word(w["x0"], w["x1"], w["top"], w["bottom"], w["text"])
                        for w in page.extract_words(keep_blank_chars=False)
                    ]
                    if (rebuilt := table_from_words(words)) is not None:
                        tables.append(rebuilt)
                        page_parts.append(f"[table]\n{_grid(rebuilt)}")

                if page_parts:
                    parts.append(f"--- page {number} ---\n" + "\n\n".join(page_parts))

            if not parts:
                return None

            # Too little text for the page count means a scan with, at most, a
            # header stamped on it.
            if characters / len(pdf.pages) < _MIN_CHARS_PER_PAGE:
                return None

            return "\n\n".join(parts)[:_MAX_TEXT_CHARS], tables
    except Exception:  # noqa: BLE001 - any failure just means "no text layer"
        logger.info("no usable text layer", exc_info=True)
        return None


# ── a table, from where the words sit ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class Word:
    """One word on a page, with where it is. What pdfplumber and an OCR
    engine both produce, reduced to the four numbers the builder needs."""

    x0: float
    x1: float
    top: float
    bottom: float
    text: str


#: Heading words that mark a line as the header of a price table. A header
#: needs something that names the item and something that names a price.
_HEAD_ITEM: Final = re.compile(
    r"^(description|item|items|particulars|product|material|details|part|model|"
    r"sku|code|article|s\.?\s*no|sr\.?\s*no|#)\b",
    re.I,
)
_HEAD_MONEY: Final = re.compile(
    r"^(unit|price|rate|amount|total|value|cost|ext\.?|extended|net)\b", re.I
)
#: A line that is a labelled field — "Validity: 30 days" — is not a row of the
#: table, however far down the page the table ran.
_LABELLED: Final = re.compile(r"^\s*[A-Za-z][A-Za-z /&.()]{1,40}:\s*\S")
_SUMMARY: Final = re.compile(
    r"\b(sub\s*-?\s*total|grand\s+total|total|vat|tax|gst|freight|shipping|discount)\b",
    re.I,
)
_NUMBER: Final = re.compile(r"^[($]?-?[\d.,]+\)?%?$")


def _lines(words: list[Word]) -> list[list[Word]]:
    """Words grouped into lines by their vertical position, left to right."""
    ordered = sorted(words, key=lambda w: (w.top, w.x0))
    lines: list[list[Word]] = []
    for word in ordered:
        if not word.text.strip():
            continue
        height = word.bottom - word.top
        if lines and abs(lines[-1][0].top - word.top) <= max(2.0, height * 0.5):
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def _cells(line: list[Word]) -> list[tuple[float, float, str]]:
    """Adjacent words joined into cells. A gap wider than about two characters
    is a column boundary; anything narrower is a space inside a heading."""
    cells: list[tuple[float, float, str]] = []
    for word in line:
        if cells:
            x0, x1, text = cells[-1]
            gap = word.x0 - x1
            height = word.bottom - word.top
            if gap < max(4.0, height * 0.9):
                cells[-1] = (x0, word.x1, f"{text} {word.text}")
                continue
        cells.append((word.x0, word.x1, word.text))
    return cells


def _is_header(cells: list[tuple[float, float, str]]) -> bool:
    if len(cells) < 3:
        return False
    texts = [c[2].strip() for c in cells]
    if any(_NUMBER.match(t) for t in texts):
        return False
    return any(_HEAD_ITEM.match(t) for t in texts) and any(
        _HEAD_MONEY.match(t) for t in texts
    )


def table_from_words(words: list[Word]) -> list[list[str]] | None:
    """The price table on a page, rebuilt from word positions.

    The column headings decide where the columns are: each heading owns the
    span from halfway to its left neighbour to halfway to its right one, and
    every word on the lines below falls into whichever span holds its centre.
    A line with words only under the description headings is a wrapped
    description and joins the row above it.

    The table ends at the first labelled line ("Payment: 30 days") or after
    two lines in a row carrying no figure at all, whichever comes first — so
    the terms printed under a quote are not read as rows of it.
    """
    lines = _lines(words)
    for index, line in enumerate(lines):
        header = _cells(line)
        if not _is_header(header):
            continue
        spans = _spans(header)
        description_at = _description_column(header)
        rows: list[list[str]] = [[text for _, _, text in header]]
        blank_run = 0
        for below in lines[index + 1 :]:
            joined = " ".join(w.text for w in below)
            if _LABELLED.match(joined) and not _SUMMARY.search(joined):
                break
            if not any(_NUMBER.match(w.text) for w in below):
                # A wrapped description, if it sits under the item columns.
                columns = {_column(spans, w) for w in below}
                if len(rows) > 1 and columns and max(columns) <= description_at:
                    target = max(columns)
                    rows[-1][target] = f"{rows[-1][target]} {joined}".strip()
                    continue
                blank_run += 1
                if blank_run >= 2:
                    break
                continue
            blank_run = 0
            row = [""] * len(spans)
            for word in below:
                column = _column(spans, word)
                row[column] = f"{row[column]} {word.text}".strip()
            rows.append(row)
        if len(rows) >= 2:
            return rows
    return None


def _spans(header: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    """Each heading's column, as the x range it owns."""
    spans: list[tuple[float, float]] = []
    for i, (x0, x1, _) in enumerate(header):
        left = 0.0 if i == 0 else (header[i - 1][1] + x0) / 2
        right = float("inf") if i == len(header) - 1 else (x1 + header[i + 1][0]) / 2
        spans.append((left, right))
    return spans


def _column(spans: list[tuple[float, float]], word: Word) -> int:
    centre = (word.x0 + word.x1) / 2
    for i, (left, right) in enumerate(spans):
        if left <= centre < right:
            return i
    return len(spans) - 1


def _description_column(header: list[tuple[float, float, str]]) -> int:
    """The last heading that names the item rather than a number."""
    last = 0
    for i, (_, _, text) in enumerate(header):
        if _HEAD_ITEM.match(text.strip()):
            last = i
    return last


# ── OCR, when an engine is installed ───────────────────────────────────


def ocr_available() -> bool:
    """Whether a scan or a photograph can be read here at all."""
    try:
        import rapidocr_onnxruntime  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def _ocr(content: bytes, media_type: str) -> tuple[str, list[list[list[str]]]] | None:
    """A scan or a photograph as text and tables, through RapidOCR.

    Optional on purpose. The engine is a wheel with its own runtime, and a
    deployment without it must still start, read every digital PDF, and say
    clearly that a scan has to be typed in. Installed, it turns the page into
    positioned words and the positional builder above does the rest — so a
    photographed quote is read by exactly the code a printed one is.
    """
    if not ocr_available():
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]

        images = _page_images(content, media_type)
        engine = RapidOCR()
        parts: list[str] = []
        tables: list[list[list[str]]] = []
        recognised = 0
        for number, image in enumerate(images, start=1):
            result, _ = engine(image)
            words: list[Word] = []
            for box, text, _score in result or []:
                xs = [pt[0] for pt in box]
                ys = [pt[1] for pt in box]
                # OCR returns phrases; split them so the column builder sees
                # words with their own x positions.
                phrase = str(text).strip()
                if not phrase:
                    continue
                x0, x1, top, bottom = min(xs), max(xs), min(ys), max(ys)
                width = (x1 - x0) / max(1, len(phrase))
                cursor = x0
                for piece in phrase.split():
                    span = width * len(piece)
                    words.append(Word(cursor, cursor + span, top, bottom, piece))
                    cursor += span + width
            recognised += len(words)
            lines = _lines(words)
            page_parts = ["\n".join(" ".join(w.text for w in line) for line in lines)]
            if (table := table_from_words(words)) is not None:
                tables.append(table)
                page_parts.append(f"[table]\n{_grid(table)}")
            parts.append(f"--- page {number} ---\n" + "\n\n".join(page_parts))
        if not parts or recognised < 5:
            # A blank page, or one the engine could make nothing of.
            return None
        return "\n\n".join(parts)[:_MAX_TEXT_CHARS], tables
    except Exception:  # noqa: BLE001 - OCR failing means "cannot read", not a crash
        logger.info("OCR failed; the document will have to be typed in", exc_info=True)
        return None


#: The longest side of a page image handed to the engine. Measured on a
#: dense A4 page: at 1,684 px the words in a box ran together and the page
#: took 41 s; at 760 px they came apart and it took 25 s. Around 1,100 px
#: reads cleanly on a normal quotation and keeps the wait tolerable.
_OCR_LONG_SIDE: Final = 1100
#: Pages read per document. A scanned tender can run to forty; the prices
#: are on the first few, and forty pages of OCR is ten minutes of waiting.
_OCR_MAX_PAGES: Final = 4


def _page_images(content: bytes, media_type: str) -> list:
    """The pages as images an OCR engine takes (numpy arrays)."""
    import numpy as np  # type: ignore[import-not-found]
    from PIL import Image

    def fit(image):
        longest = max(image.width, image.height)
        if longest > _OCR_LONG_SIDE:
            ratio = _OCR_LONG_SIDE / longest
            image = image.resize((int(image.width * ratio), int(image.height * ratio)))
        return np.array(image.convert("RGB"))

    if media_type in PDF_TYPES:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]

        document = pdfium.PdfDocument(content)
        return [
            fit(document[i].render(scale=1.4).to_pil())
            for i in range(min(len(document), _OCR_MAX_PAGES))
        ]
    return [fit(Image.open(io.BytesIO(content)))]


# ── conversions ────────────────────────────────────────────────────────


def _grid(rows: list[list[str]]) -> str:
    """Rows as a pipe-delimited table, blank rows dropped.

    A grid keeps each price under its own heading, which is the one structural
    fact a reader needs to read a quote correctly.
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
                # A sheet already *is* a table, so it is offered to the parser
                # as one rather than only as flattened text.
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
