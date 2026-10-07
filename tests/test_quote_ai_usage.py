"""Which model read a quote's documents, and what it cost. No network and no
database: the providers answer from a script (see test_free_model_first)."""

from __future__ import annotations

from decimal import Decimal

from app.core import llm
from app.core.llm import ModelError
from tests.test_free_model_first import Scripted

GOOD = '{"supplier_name": "Acme", "total": 100}'


async def test_every_answer_is_kept_with_its_model_and_label() -> None:
    model = Scripted({"groq": GOOD})
    with llm.capture() as calls:
        await model.extract(instructions="i", text="t", shape={}, label="Supplier quote · a.pdf")
    assert [(c.provider, c.model, c.label, c.used) for c in calls] == [
        ("groq", "gpt-oss-120b", "Supplier quote · a.pdf", True)
    ]
    assert calls[0].cost_usd == 0  # a free tier


async def test_an_unusable_answer_is_kept_as_paid_for_and_not_used() -> None:
    """Groq answers rubbish, Claude is asked: two calls, and the first one's
    tokens were spent all the same."""
    model = Scripted({"groq": "not json at all", "anthropic": GOOD})
    with llm.capture() as calls:
        await model.ask(system="s", user="u")
    assert [(c.provider, c.used) for c in calls] == [("groq", False), ("anthropic", True)]


async def test_claude_is_priced_at_its_list_price() -> None:
    model = Scripted({"groq": ModelError("429"), "anthropic": GOOD})
    with llm.capture() as calls:
        await model.ask(system="s", user="u")
    # The scripted provider reports 100 tokens in and 20 out; Haiku is $1 / $5.
    assert len(calls) == 1
    assert calls[0].cost_usd == Decimal("0.0002")


def test_an_unknown_claude_model_is_never_free() -> None:
    assert llm.cost_of("anthropic", "claude-something-new", 1_000_000, 0) == Decimal(5)
    assert llm.cost_of("groq", "anything", 1_000_000, 1_000_000) == 0


async def test_calls_outside_a_capture_are_not_collected() -> None:
    model = Scripted({"groq": GOOD})
    with llm.capture() as calls:
        pass
    await model.ask(system="s", user="u")
    assert calls == []
