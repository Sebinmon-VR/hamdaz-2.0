"""No model where none is needed, the free model next, and Claude last.

No network: the providers are stubbed at the one place a request leaves,
``TextModel._ask``, so what is tested is the order they are asked in and what
makes one give way to the next.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.llm import ModelError, Provider, TextModel
from app.intake.classifier import Classifier, ClassifierError, without_a_model
from app.models.intake import MailCategory

GROQ = Provider("groq", "https://groq.invalid", "k", "gpt-oss-120b")
CLAUDE = Provider("anthropic", "https://claude.invalid", "k", "claude-haiku-4-5")


def settings(**overrides: Any) -> Any:
    return SimpleNamespace(
        llm_max_input_chars=24_000,
        anthropic_workspace_id="",
        anthropic_model="claude-sonnet-5",
        **overrides,
    )


class Scripted(TextModel):
    """A text model whose providers answer from a script, recording who was asked."""

    def __init__(self, replies: dict[str, Any]) -> None:
        super().__init__(settings(llm_providers=""), http=None)  # type: ignore[arg-type]
        self._providers = [GROQ, CLAUDE]
        self.replies = replies
        self.asked: list[tuple[str, str]] = []

    async def _ask(self, provider, prompt, max_tokens):
        self.asked.append((provider.name, provider.model))
        reply = self.replies.get(provider.name)
        if isinstance(reply, Exception):
            raise reply
        return reply, 100, 20


TENDER = (
    '{"category": "tender", "confidence": 0.9, "is_reopened": false, "title": "Pumps", '
    '"customer": "ADNOC", "references": ["RFQ 6000151129"], "deadline": "2026-10-20", '
    '"summary": "Two pumps.", "reasoning": "An invitation to bid."}'
)


async def classify(model: TextModel, subject: str = "RFQ 6000151129 - pumps", **kw) -> Any:
    return await Classifier(settings(), model).classify(
        subject=subject,
        body=kw.get("body", "Please quote for two pumps."),
        sender=kw.get("sender", "buyer@adnoc.ae"),
    )


@pytest.mark.parametrize(
    ("subject", "sender", "body"),
    [
        ("Automatic reply: RFQ 6000151129", "buyer@adnoc.ae", ""),
        ("Undeliverable: Quotation", "postmaster@hamdaz.com", ""),
        ("Accepted: Daily meeting", "sebin@hamdaz.com", ""),
        ("Re: your quote", "MAILER-DAEMON@mx.example.com", ""),
        ("Re: pumps", "buyer@adnoc.ae", "I am currently out of the office until Sunday."),
    ],
)
async def test_automatic_mail_is_settled_with_no_model_at_all(subject, sender, body) -> None:
    model = Scripted({})
    result = await classify(model, subject, sender=sender, body=body)
    assert result.category == MailCategory.GENERAL
    assert model.asked == []
    assert result.cost_usd == 0.0


def test_a_rule_never_says_what_is_work() -> None:
    """Rules may only settle mail as not-work; a tender always goes to a model."""
    assert without_a_model(subject="RFQ 6000151129", body="Kindly quote", sender="a@b.c") is None


async def test_the_free_model_is_asked_first_and_claude_not_at_all() -> None:
    model = Scripted({"groq": TENDER, "anthropic": TENDER})
    result = await classify(model)
    assert model.asked == [("groq", "gpt-oss-120b")]
    assert result.category == MailCategory.TENDER
    assert result.cost_usd == 0.0
    assert "groq" in result.reasoning


async def test_a_rate_limit_on_the_free_model_falls_to_claude() -> None:
    model = Scripted({"groq": ModelError("groq answered 429"), "anthropic": TENDER})
    result = await classify(model)
    assert [name for name, _ in model.asked] == ["groq", "anthropic"]
    assert result.category == MailCategory.TENDER
    assert result.cost_usd > 0


async def test_an_answer_outside_the_shape_falls_to_claude() -> None:
    vague = '{"category": "maybe a tender", "confidence": 0.9}'
    model = Scripted({"groq": vague, "anthropic": TENDER})
    result = await classify(model)
    assert [name for name, _ in model.asked] == ["groq", "anthropic"]
    assert result.category == MailCategory.TENDER


async def test_nobody_answering_is_an_error_the_pipeline_records() -> None:
    model = Scripted({"groq": ModelError("down"), "anthropic": ModelError("no credit")})
    with pytest.raises(ClassifierError):
        await classify(model)


async def test_claude_can_be_asked_on_a_different_model_and_the_free_one_alone() -> None:
    model = Scripted({"groq": ModelError("429"), "anthropic": '{"item_id": ""}'})
    await model.ask(system="s", user="u", claude_model="claude-sonnet-5")
    assert model.asked[-1] == ("anthropic", "claude-sonnet-5")

    model = Scripted({"groq": ModelError("429"), "anthropic": '{"item_id": ""}'})
    assert await model.ask(system="s", user="u", free_only=True) is None
    assert [name for name, _ in model.asked] == ["groq"]
