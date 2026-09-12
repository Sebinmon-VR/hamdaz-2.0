"""Reading requirement documents into a fixed shape.

The comparison module reads *supplier quotes* — one document, one known
layout, priced lines. A requirement document is anything: a tender, an RFQ
mail printed to PDF, a spreadsheet of items, a drawing with a parts list. So
this reads every document on a run in one call and asks for one answer in the
shape ``catalogue.REQUIREMENTS_SCHEMA`` describes, forced through a tool so
the answer is always that shape and never a paragraph.

Same client, same key and same conversion as the comparison module — a
document is prepared by ``app.comparison.documents.prepare`` and reaches the
model as a document block, an image block, or text — so a file the comparison
screen can read, this can read.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import anthropic

from app.comparison.documents import Readable
from app.core.config import Settings

logger = logging.getLogger("hamdaz.workflows.reader")

#: Documents per call. A tender pack is a few files; a hundred is a mistake.
MAX_DOCUMENTS = 12


class ReaderError(Exception):
    """The documents could not be read. The message is written for a person."""


def _block(readable: Readable) -> dict[str, Any]:
    if readable.kind == "document":
        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": readable.media_type,
                "data": base64.standard_b64encode(readable.data or b"").decode(),
            },
            "title": readable.file_name,
        }
    if readable.kind == "image":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": readable.media_type,
                "data": base64.standard_b64encode(readable.data or b"").decode(),
            },
        }
    return {"type": "text", "text": f"=== {readable.file_name} ===\n{readable.text or ''}"}


def _tool_input(response: Any) -> dict[str, Any] | None:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "tool_use":
            payload = getattr(block, "input", None)
            return dict(payload) if isinstance(payload, dict) else None
    return None


def _cost(response: Any, settings: Settings) -> float:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    price_in = float(getattr(settings, "anthropic_input_price", 0) or 0)
    price_out = float(getattr(settings, "anthropic_output_price", 0) or 0)
    return (inp * price_in + out * price_out) / 1_000_000


class DocumentReader:
    """One call per run: every document in, one structured answer out."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def configured(self) -> bool:
        return self._settings.claude_configured

    def _anthropic(self) -> anthropic.AsyncAnthropic:
        if not self.configured:
            raise ReaderError(
                "Reading documents needs an Anthropic API key (ANTHROPIC_API_KEY). "
                "The items can still be typed in by hand."
            )
        if self._client is None:
            headers = {}
            if self._settings.anthropic_workspace_id:
                headers["anthropic-workspace-id"] = self._settings.anthropic_workspace_id
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key, default_headers=headers or None
            )
        return self._client

    async def read(
        self,
        readables: list[Readable],
        *,
        schema: dict[str, Any],
        instructions: str = "",
        typed_items: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], float]:
        """The documents, read into ``schema``. Returns the answer and its cost."""
        if not readables and not typed_items:
            raise ReaderError("There are no documents to read.")
        if len(readables) > MAX_DOCUMENTS:
            raise ReaderError(f"At most {MAX_DOCUMENTS} documents can be read at once.")

        content: list[dict[str, Any]] = [_block(r) for r in readables]
        ask = (
            "Read the documents above and record what the customer needs priced. "
            "One entry per distinct item; quantities as stated, 1 when not stated. "
            "Copy part numbers and brands exactly. Put anything that is a condition "
            "rather than an item under requirements, and what a supplier would have "
            "to ask about under missing. Use an empty string for text that is not "
            "there — never invent."
        )
        if typed_items:
            ask += (
                "\n\nThe person also listed these items by hand; include them, merged "
                "with what the documents say, without duplicating:\n"
                + "\n".join(f"- {i}" for i in typed_items)
            )
        content.append({"type": "text", "text": ask})

        system = (
            "You read requirement documents for a trading and contracting company "
            "that quotes customers for equipment and materials. "
            + (instructions or "")
        ).strip()
        try:
            response = await self._anthropic().messages.create(
                model=self._settings.extract_model,
                max_tokens=6000,
                system=system,
                tools=[
                    {
                        "name": "record",
                        "description": "Record what the documents ask for.",
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": "record"},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIStatusError as exc:
            raise ReaderError(f"The document reader refused ({exc.status_code}): {exc.message}") from exc
        except Exception as exc:  # noqa: BLE001 - surfaced on the run
            raise ReaderError(f"{type(exc).__name__}: {exc}") from exc

        payload = _tool_input(response)
        if payload is None:
            raise ReaderError("The model did not answer in the expected shape.")
        return payload, _cost(response, self._settings)
