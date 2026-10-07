"""AI employees: how one narrows the assistant. No database, no model."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.assistant import employees
from app.assistant.catalogue import LIVE_TOOLS
from app.assistant.policy import Actor, ResolvedTool
from app.assistant.service import Snapshot
from app.models.ai_employee import AIEmployee, WriteMode


def _employee(**kw) -> AIEmployee:
    base = dict(
        id=uuid.uuid4(), name="Nora", title="Pre-sales engineer", description="Prices enquiries.",
        instructions="Never quote a price without a supplier quote.", allowed_modules=[],
        write_mode=WriteMode.READ_ONLY, audience_roles=[], enabled=True, model_key=None,
        reasoning_effort=None, monthly_budget_usd=None,
    )
    base.update(kw)
    return AIEmployee(**base)


def _actor(*roles: str) -> Actor:
    return Actor(user_id=uuid.uuid4(), roles=frozenset(roles), team_ids=frozenset(), access_modules=frozenset())


def _tools() -> list[ResolvedTool]:
    return [ResolvedTool(spec, False) for spec in LIVE_TOOLS]


def test_read_only_withholds_every_write() -> None:
    narrowed = employees.narrow_tools(_tools(), _employee())
    assert narrowed and not any(t.spec.is_write for t in narrowed)


def test_confirm_makes_every_write_wait() -> None:
    narrowed = employees.narrow_tools(_tools(), _employee(write_mode=WriteMode.CONFIRM))
    writes = [t for t in narrowed if t.spec.is_write]
    assert writes and all(t.requires_confirmation for t in writes)


def test_policy_leaves_the_assistants_choice() -> None:
    tools = _tools()
    narrowed = employees.narrow_tools(tools, _employee(write_mode=WriteMode.POLICY))
    assert [(t.spec.key, t.requires_confirmation) for t in narrowed] == [
        (t.spec.key, t.requires_confirmation) for t in tools
    ]


def test_modules_narrow_but_the_app_module_stays() -> None:
    narrowed = employees.narrow_tools(_tools(), _employee(allowed_modules=["leave"]))
    modules = {t.spec.module_key for t in narrowed}
    assert modules <= {"leave", "app"} and "leave" in modules and "app" in modules


def test_nothing_is_widened() -> None:
    few = _tools()[:5]
    assert len(employees.narrow_tools(few, _employee(write_mode=WriteMode.POLICY))) == 5


def test_audience() -> None:
    open_to_all = _employee()
    managers = _employee(audience_roles=["manager"])
    assert employees.may_talk(open_to_all, _actor())
    assert not employees.may_talk(managers, _actor("accountant"))
    assert employees.may_talk(managers, _actor("manager"))
    assert employees.may_talk(managers, _actor("super_admin"))
    assert not employees.may_talk(_employee(enabled=False), _actor("super_admin"))


async def test_apply_overrides_model_and_effort_without_touching_the_cache() -> None:
    base = SimpleNamespace(model_key="gpt-5.6-terra", reasoning_effort="low", max_tool_rounds=8)
    snapshot = Snapshot(settings=base, model=SimpleNamespace(key="gpt-5.6-terra"),
                        module_policies={}, tool_policies={}, rules=[])
    claude = SimpleNamespace(key="claude-sonnet-5-5", enabled=True)

    class FakeSession:
        async def get(self, _cls, key):
            return claude if key == "claude-sonnet-5-5" else None

    applied = await employees.apply(
        FakeSession(), snapshot, _employee(model_key="claude-sonnet-5-5", reasoning_effort="high")
    )
    assert applied.settings.model_key == "claude-sonnet-5-5"
    assert applied.settings.reasoning_effort == "high"
    assert applied.settings.max_tool_rounds == 8
    assert applied.model is claude
    assert base.model_key == "gpt-5.6-terra" and snapshot.model.key == "gpt-5.6-terra"

    unchanged = await employees.apply(FakeSession(), snapshot, _employee(model_key="no-such-model"))
    assert unchanged is snapshot


def test_persona_carries_name_job_rules_and_write_mode() -> None:
    text = employees.persona(_employee())
    assert "Nora" in text and "Pre-sales engineer" in text and "an AI employee" in text
    assert "Never quote a price" in text and "not change anything" in text
    assert "confirm" in employees.persona(_employee(write_mode=WriteMode.CONFIRM))
