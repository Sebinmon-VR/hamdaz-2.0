"""Reading a supplier quote.

Two readers, tried in order:

1. **The local parser** (``parsing.py``). Most supplier quotes are machine
   generated and entirely regular — a table with a description column and a
   price column, and labelled fields around it. Reading one of those is
   deterministic work, and deterministic work should not cost money, vary
   between runs, or need a network call.

2. **Claude**, only for what defeats the parser: scans, photographs, layouts
   with no recognisable price table, and tables the parser could only partly
   explain. It declines loudly rather than guessing, so this path is reached on
   purpose rather than by accident.

Either way the model never does arithmetic. It transcribes; the totals are
computed in Python from what it read, because a figure a model added up is a
figure nobody can check.

**Cost.** The system prompt and schema are identical on every call and are
marked for caching, so a comparison with four suppliers pays for that prefix
once. Extraction runs at ``medium`` effort — reading a document is
transcription, not reasoning — and may run on a cheaper model than the matching
step; see ``Settings.anthropic_extract_model``.

**Honesty over completeness.** Both readers leave a field blank rather than
guess, and say so in ``note``. A missing delivery time is a fact a reviewer can
act on; an invented one is a fact they cannot.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Final

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.comparison.documents import Readable
from app.core.config import Settings

logger = logging.getLogger("hamdaz.comparison")

#: Long enough for a quote with a hundred lines; short enough that a runaway
#: response cannot cost real money.
_MAX_TOKENS: Final = 16000

#: Documents in one comparison are independent, so they are read together. Held
#: below the account's concurrency to leave room for other traffic.
_MAX_PARALLEL: Final = 4

_SYSTEM: Final = """\
You read supplier quotations and transcribe them into structured data. You work \
for a procurement team that will compare several of these side by side and place \
an order based on the result.

Rules, in order of importance:

1. Transcribe, never infer. Every field is required, so when a value is not on \
the document write "" for text and 0 for an amount. A blank is useful; a \
plausible guess is dangerous, because nobody downstream can tell it apart from a \
real reading.
2. Never calculate. Copy quantity, unit price and line total exactly as printed, \
even when they disagree with each other. The disagreement is information — it \
usually means a discount or a fee, and it is checked downstream.
3. Copy prices digit for digit. Watch for thousands separators that are commas \
in one quote and full stops in another, and for prices printed per-pack when the \
quantity is per-unit.
4. One entry per priced line. Do not merge lines, do not split them, and do not \
include subtotal, freight, VAT or grand-total rows as line items — those belong \
in the header fields.
5. Record what you were unsure of in `note`: an illegible figure, a line with no \
price, two prices for one item, a currency you had to infer from a symbol. Say \
which line. This is read by a person before they trust the rest.
"""


# Every field below is REQUIRED and plainly typed. Both matter, and the first one
# is not obvious — it was measured:
#
#   15 flat fields WITH defaults  -> "Schema is too complex", after 185 seconds
#   15 flat fields, all required  -> fine, 7 seconds
#   a nested list of 8 fields     -> fine, 6 seconds
#
# A field with a default is *optional* in JSON Schema, and structured output
# compiles the schema into a decoding grammar. Every optional field doubles the
# set of valid key sequences the grammar must admit, so a dozen of them is a
# combinatorial explosion — the nested list was never the problem. Required
# fields have exactly one valid ordering, and the grammar stays trivial.
#
# The cost is that the model must emit every key, so "" and 0 carry the meaning
# "not on the document". :func:`blank_to_none` turns them back into nulls, and
# the system prompt tells the model the same convention.
#
# Class docstrings are short on purpose too: Pydantic puts them in the schema,
# and the schema goes over the wire on every call.


class ExtractedItem(BaseModel):
    """One priced line from a quotation."""

    description: str = Field(description="The item as written on the quote")
    part_number: str = Field(description="Supplier or manufacturer part number")
    brand: str = Field(description="Make or manufacturer, if named")
    unit: str = Field(description="each, set, metre, box...")
    quantity: float = Field(
        description="As printed. 1 if the quote shows no quantity"
    )
    unit_price: float = Field(description="Price for ONE unit, as printed")
    line_total: float = Field(
        description="The line total as PRINTED. 0 if the quote shows none"
    )
    lead_time: str = Field(description="Lead time for this line, if per-line")


class ExtractedQuote(BaseModel):
    """One supplier's quotation."""

    supplier_name: str = Field(description="The company that issued the quote")
    quote_number: str = Field(description="Quote or offer number")
    quote_date: str = Field(description="As printed, any format")
    currency: str = Field(
        description="ISO code such as AED, USD, EUR. Infer from a symbol if needed"
    )
    validity: str = Field(description="How long the price holds")
    delivery_time: str = Field(description="Delivery or lead time")
    payment_terms: str = Field(description="Payment terms")
    warranty: str = Field(description="Warranty or guarantee")
    incoterms: str = Field(description="EXW, FOB, CIF, DDP...")
    contact: str = Field(description="Name or email of the sender")

    discount: float = Field(description="Total discount, if shown separately")
    freight: float = Field(description="Shipping or handling, if shown")
    tax: float = Field(description="VAT or tax, if shown")
    quoted_total: float = Field(
        description="The grand total as PRINTED. Do not compute it"
    )

    items: list[ExtractedItem] = Field(description="Every priced line on the quote")
    note: str = Field(
        description="Anything uncertain or unreadable, naming the line"
    )


def blank_to_none(value):
    """``""`` and ``0`` mean "not on the document", not "zero".

    The schema uses blanks so the grammar stays simple; this restores the
    distinction that matters downstream, where a missing delivery time and an
    empty one read very differently.
    """
    if value == "" or value == 0:
        return None
    return value


#: The shape asked for in words when constrained decoding is unavailable.
#: Kept beside the models so the two cannot drift apart.
_JSON_SHAPE = {
    "supplier_name": "string",
    "quote_number": "string",
    "quote_date": "string",
    "currency": "ISO code, e.g. AED",
    "validity": "string",
    "delivery_time": "string",
    "payment_terms": "string",
    "warranty": "string",
    "incoterms": "string",
    "contact": "string",
    "discount": 0,
    "freight": 0,
    "tax": 0,
    "quoted_total": 0,
    "items": [
        {
            "description": "string",
            "part_number": "string",
            "brand": "string",
            "unit": "string",
            "quantity": 0,
            "unit_price": 0,
            "line_total": 0,
            "lead_time": "string",
        }
    ],
    "note": "anything uncertain, naming the line",
}


class ExtractionError(Exception):
    """A document could not be read. Safe to show a user."""


class QuoteExtractor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: anthropic.AsyncAnthropic | None = None
        self._gate = asyncio.Semaphore(_MAX_PARALLEL)

    @property
    def configured(self) -> bool:
        return self._settings.claude_configured

    def _anthropic(self) -> anthropic.AsyncAnthropic:
        if not self.configured:
            raise ExtractionError(
                "Reading quote documents needs an Anthropic API key. Create one at "
                "console.anthropic.com and set ANTHROPIC_API_KEY. Suppliers can still "
                "be entered by hand in the meantime."
            )
        if self._client is None:
            # Set explicitly rather than left to the SDK's credential chain:
            # passing api_key= bypasses the chain, so ANTHROPIC_WORKSPACE_ID
            # would never reach the wire on its own.
            headers = {}
            if self._settings.anthropic_workspace_id:
                headers["anthropic-workspace-id"] = self._settings.anthropic_workspace_id
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key,
                default_headers=headers or None,
            )
        return self._client

    def _content(self, readable: Readable) -> list[dict]:
        """The document, then the instruction. Order matters — the API wants the
        document block before the text that refers to it."""
        import base64

        instruction = {
            "type": "text",
            "text": (
                f"Transcribe this supplier quotation ({readable.file_name}). "
                f'Every field is required: use "" for text that is not on the '
                f"document and 0 for an amount that is not. `items` must contain "
                f"one entry for every priced line — if you return none, say why "
                f"in `note`."
            ),
        }

        if readable.kind == "document":
            return [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": readable.media_type,
                        "data": base64.standard_b64encode(readable.data or b"").decode(),
                    },
                },
                instruction,
            ]
        if readable.kind == "image":
            return [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": readable.media_type,
                        "data": base64.standard_b64encode(readable.data or b"").decode(),
                    },
                },
                instruction,
            ]
        return [
            {
                "type": "text",
                "text": (
                    f"Supplier quotation, converted from {readable.file_name}. "
                    f"Rows are pipe-delimited.\n\n{readable.text}"
                ),
            },
            instruction,
        ]

    async def read(self, readable: Readable) -> ExtractedQuote:
        """One document in, one validated quote out.

        Tries to parse it locally first. Most supplier quotes are machine
        generated and entirely regular, and parsing one of those is deterministic
        work that should not cost money or vary between runs. The model is for
        the documents that defeat it — scans, unusual layouts, tables with no
        recognisable price column.
        """
        if (parsed := parse_locally(readable)) is not None:
            return parsed
        return await self.read_with_model(readable)

    async def read_with_model(self, readable: Readable) -> ExtractedQuote:
        """Straight to Claude, skipping the local parser."""
        client = self._anthropic()

        async with self._gate:
            try:
                response = await self._parse_call(client, readable)
            except anthropic.APIStatusError as exc:
                detail = str(getattr(exc, "message", "") or exc)
                # Constrained decoding refused the schema. The model can still
                # read the document perfectly well — only the grammar failed —
                # so ask for plain JSON and validate it here rather than lose
                # the extraction to a decoding detail.
                if exc.status_code == 400 and (
                    "too complex" in detail.lower() or "grammar" in detail.lower()
                ):
                    logger.warning(
                        "structured output refused for %s (%s); retrying as plain JSON",
                        readable.file_name,
                        detail[:80],
                    )
                    response = await self._json_call(client, readable)
                else:
                    raise ExtractionError(self._explain(exc, readable.file_name)) from exc
            except anthropic.APIConnectionError as exc:
                raise ExtractionError("Could not reach Claude to read the document") from exc

        usage = response.usage
        logger.info(
            "extracted %s in=%s cached=%s out=%s",
            readable.file_name,
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.output_tokens,
        )

        quote = getattr(response, "parsed_output", None) or self._quote_from_text(
            response, readable.file_name
        )
        if quote is None:
            raise ExtractionError(f"Claude returned nothing readable for {readable.file_name!r}")
        return quote

    def _request(self, readable: Readable) -> dict:
        """The half of the request that is the same either way."""
        return {
            # Extraction may run on a cheaper model than matching — see
            # Settings.anthropic_extract_model for why.
            "model": self._settings.extract_model,
            "max_tokens": _MAX_TOKENS,
            # Identical on every call, so four suppliers pay for the prompt
            # once. The document, which varies, comes after it.
            "system": [
                {
                    "type": "text",
                    "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "output_config": {"effort": self._settings.anthropic_extract_effort},
            "messages": [{"role": "user", "content": self._content(readable)}],
        }

    async def _parse_call(self, client, readable: Readable):
        """The normal path: the API guarantees the shape."""
        return await client.messages.parse(
            **self._request(readable), output_format=ExtractedQuote
        )

    async def _json_call(self, client, readable: Readable):
        """The fallback: ask for JSON in words, and validate it here.

        Loses the API's guarantee, so the response is parsed defensively — but a
        quote read from a document Claude understood is worth far more than a
        clean failure over a grammar the compiler would not build.
        """
        request = self._request(readable)
        request["messages"] = [
            {
                "role": "user",
                "content": [
                    *self._content(readable),
                    {
                        "type": "text",
                        "text": (
                            "Reply with a single JSON object and nothing else — no "
                            "prose, no code fence. Use exactly these keys:\n"
                            + json.dumps(_JSON_SHAPE, indent=1)
                            + '\nUse "" for any text not on the document, and 0 '
                            "for any amount not on it."
                        ),
                    },
                ],
            }
        ]
        return await client.messages.create(**request)

    def _quote_from_text(self, response, file_name: str) -> ExtractedQuote | None:
        """Validate the JSON out of a fallback response."""
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return ExtractedQuote.model_validate_json(text[start : end + 1])
        except (ValueError, ValidationError) as exc:
            logger.warning("fallback JSON did not validate for %s: %s", file_name, exc)
            return None

    def _explain(self, exc: anthropic.APIStatusError, file_name: str) -> str:
        """Turn an API error into something a person can act on.

        The workspace case is called out by name because the message Anthropic
        returns is accurate but says nothing about where to find the id, and it
        is the first thing an identity-linked key hits.
        """
        detail = str(getattr(exc, "message", "") or exc)

        if "anthropic-workspace-id" in detail:
            return (
                "This Anthropic key is identity-linked, so every request must name "
                "the workspace it acts in. Find it in the Anthropic Console under "
                "Settings -> Workspaces (an id beginning 'wrkspc_') and set "
                "ANTHROPIC_WORKSPACE_ID in .env, then restart."
            )
        if exc.status_code == 401:
            return "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env."
        if exc.status_code == 429:
            return "Anthropic is rate limiting this key. Try again shortly."
        if exc.status_code == 400 and "credit balance" in detail.lower():
            return (
                "This Anthropic account has no credit. Add some at "
                "console.anthropic.com -> Billing; extraction is billed per token."
            )
        return f"Claude could not read {file_name!r} ({exc.status_code}). {detail}"

    def _local_only(self, readable: Readable) -> ExtractedQuote | ExtractionError:
        """Local parse, or an error explaining why the model was needed."""
        if (parsed := parse_locally(readable)) is not None:
            return parsed
        return ExtractionError(
            f"{readable.file_name!r} could not be read without AI, and no Anthropic "
            f"key is configured. Set ANTHROPIC_API_KEY, or enter this supplier by hand."
        )

    async def read_all(
        self, readables: list[Readable]
    ) -> list[ExtractedQuote | ExtractionError]:
        """Every document at once, each failure kept with its own file.

        Returned rather than raised: one unreadable scan among four suppliers
        should leave the other three usable, not lose the whole upload.
        """
        # Parsed locally first, so an unconfigured key still gets real results
        # from every regular quote and only fails on the ones that need vision.
        if not self.configured:
            return [self._local_only(r) for r in readables]

        results = await asyncio.gather(
            *(self.read(r) for r in readables), return_exceptions=True
        )
        out: list[ExtractedQuote | ExtractionError] = []
        for readable, result in zip(readables, results, strict=True):
            if isinstance(result, ExtractionError):
                out.append(result)
            elif isinstance(result, BaseException):
                logger.exception("extraction failed for %s", readable.file_name)
                out.append(ExtractionError(f"Could not read {readable.file_name!r}: {result}"))
            else:
                out.append(result)
        return out


def to_decimal(value: float | str | None, default: Decimal | None = None) -> Decimal | None:
    """A model-supplied number as exact currency.

    Goes through ``str`` deliberately: ``Decimal(1234.56)`` inherits the float's
    error, ``Decimal("1234.56")`` does not. Money is compared and totalled here,
    so the difference eventually shows up in front of a supplier.
    """
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def parse_locally(readable: Readable) -> ExtractedQuote | None:
    """The local parser, imported late.

    ``parsing`` needs ``ExtractedQuote`` from this module, so importing it at the
    top would be circular. The call is cheap and happens once per document.
    """
    from app.comparison.parsing import parse

    return parse(readable)
