"""Accepting an uploaded file.

Nothing here reads a document — HR files are stored and handed back, not parsed.
So the checks are the ones that matter for storing something people will
download later: a size the database is happy with, a type a browser will not
execute, and a file name that is a name rather than a path.

The type list is an allowlist. A denylist of dangerous extensions is a list you
find out is incomplete after somebody has already downloaded the thing that was
not on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Per file. Offer letters and CVs are small; the ceiling is here to stop a
#: mistake, not to accommodate a use case.
MAX_FILE_BYTES: Final[int] = 15 * 1_048_576

#: Extension → the content type it is stored as. The browser's own
#: ``content_type`` is not trusted: it is whatever the uploading client claimed.
ALLOWED: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".rtf": "application/rtf",
}

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\-]")


class UploadError(ValueError):
    """The file was refused. The message is shown to whoever uploaded it."""


@dataclass(frozen=True, slots=True)
class Upload:
    file_name: str
    content_type: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


def safe_name(name: str) -> str:
    """A file name with nothing in it that means anything to a filesystem.

    Directory separators and traversal segments go, because this name is echoed
    back in a ``Content-Disposition`` header and may well be what somebody's
    browser saves to disk.
    """
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _UNSAFE.sub("_", base).lstrip(".") or "file"
    return base[:255]


def accept(name: str, content: bytes, declared_type: str | None = None) -> Upload:
    """Validate one upload, or raise ``UploadError`` saying why not."""
    if not content:
        raise UploadError(f"{name or 'The file'} is empty")
    if len(content) > MAX_FILE_BYTES:
        raise UploadError(
            f"{name} is {len(content) // 1_048_576}MB. The limit is "
            f"{MAX_FILE_BYTES // 1_048_576}MB per file."
        )

    clean = safe_name(name)
    suffix = clean[clean.rfind(".") :].casefold() if "." in clean else ""
    if suffix not in ALLOWED:
        raise UploadError(
            f"{clean} is not a type we accept. Allowed: "
            f"{', '.join(sorted(ALLOWED))}."
        )

    # The extension decides, not the claim. ``declared_type`` is only consulted
    # to keep a more specific but compatible value the client sent.
    resolved = ALLOWED[suffix]
    if declared_type and declared_type.split(";")[0].strip().casefold() == resolved:
        resolved = declared_type.split(";")[0].strip().casefold()
    return Upload(file_name=clean, content_type=resolved, content=content)


def download_headers(file_name: str) -> dict[str, str]:
    """Headers that make a stored file download rather than render.

    ``attachment`` on everything, including PDFs and images: a document served
    inline is a document the browser renders in the ERP's own origin, and an
    uploaded SVG or HTML that slipped past the allowlist would then be script
    running there. Downloading is also what people want from an offer letter.
    """
    safe = safe_name(file_name)
    return {
        "Content-Disposition": f'attachment; filename="{safe}"',
        "X-Content-Type-Options": "nosniff",
        # Personnel files should not sit in a shared cache.
        "Cache-Control": "private, no-store",
    }
