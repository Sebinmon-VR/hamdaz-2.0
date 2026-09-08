"""The assistant's configuration and record, against the database.

What a super admin sets has to survive a deploy, refuse to contradict itself,
and be enforced the same way however it was changed. Those are the properties
here; the pure decision logic is in test_assistant_policy.py.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.assistant import service
from app.assistant.catalogue import LIVE_TOOLS
from app.assistant.policy import Actor, admit
from app.auth.oidc import EntraIdentity
from app.auth.service import upsert_user
from app.models.assistant import AssistantSettings, AudienceMode
from app.roles import service as roles_service
from app.teams import service as teams_service


def _settings(**kw) -> AssistantSettings:
    row = AssistantSettings(id=1)
    row.enabled = kw.get("enabled", True)
    row.audience_mode = kw.get("audience_mode", AudienceMode.EVERYONE)
    row.confirm_writes_default = kw.get("confirm_writes_default", True)
    return row


@pytest.fixture
async def seeded(db):
    await roles_service.seed_system_roles(db)
    await service.seed_models(db)
    await service.seed_policies(db)
    await db.commit()


async def _user(db, email: str, *role_keys: str):
    user = await upsert_user(
        db, EntraIdentity(object_id=email, email=email, display_name=email.split("@")[0])
    )
    for key in role_keys:
        await roles_service.assign_role(db, user_id=user.id, role_key=key, granted_by_id=None)
    await db.commit()
    return user


async def test_seeding_is_idempotent(db, seeded) -> None:
    before = len(await service.list_models(db))
    modules, tools = await service.seed_policies(db)
    await service.seed_models(db)
    await db.commit()
    assert len(await service.list_models(db)) == before
    # Live tools only: a planned one has nothing to switch, and giving it a row
    # would imply it could be turned on.
    assert tools == len(LIVE_TOOLS)
    assert len(await service.tool_policies(db)) == len(LIVE_TOOLS)


async def test_seeding_ships_the_write_rules_the_catalogue_declares(db, seeded) -> None:
    """Writes on everywhere, and the company-wide ones held to the named roles.
    This is the state a fresh install starts in, so it is worth pinning."""
    modules = await service.module_policies(db)
    assert all(policy.write_enabled for policy in modules.values())
    for key in ("roles", "user_admin", "teams", "templates", "assignment", "finance", "quotes"):
        assert set(modules[key].write_roles) == {"super_admin", "ceo", "manager"}, key
    for key in ("leave", "hr", "proposals", "quote_requests", "dashboard"):
        assert modules[key].write_roles is None, key


async def test_seeding_does_not_reimpose_a_write_rule_that_was_lifted(db, seeded) -> None:
    """The catalogue's restriction is what a module ships with, not what it is
    held to for ever — otherwise every deploy would undo the decision."""
    await service.update_module_policy(
        db, "teams", actor_id=None, changes={"write_roles": []}
    )
    await db.commit()

    await service.seed_policies(db)
    await db.commit()
    assert (await service.module_policies(db))["teams"].write_roles is None


async def test_the_voice_models_are_seeded_with_prices(db, seeded) -> None:
    models = {m.key: m for m in await service.seed_voice_models(db)}
    await db.commit()
    assert models["gpt-4o-mini-tts"].char_price > 0
    assert models["gpt-realtime-2.1"].audio_output_price > 0
    # Speech is billed per character and realtime per token; neither borrows
    # the other's unit, which is why they are two kinds rather than one table.
    assert models["gpt-4o-mini-tts"].audio_output_price == 0
    assert models["gpt-realtime-2.1"].char_price == 0


async def test_seeding_voice_models_does_not_undo_a_corrected_price(db, seeded) -> None:
    await service.seed_voice_models(db)
    await service.update_voice_model(
        db, "gpt-4o-mini-tts", actor_id=None, changes={"char_price": Decimal("99")}
    )
    await db.commit()

    await service.seed_voice_models(db)
    await db.commit()
    assert (await service.get_voice_model(db, "gpt-4o-mini-tts")).char_price == Decimal("99")


async def test_seeding_does_not_undo_an_administrators_decision(db, seeded) -> None:
    """The whole point of seeding this way: a deploy must not re-open a write."""
    await service.update_module_policy(
        db, "leave", actor_id=None, changes={"write_enabled": True}
    )
    await service.update_model(db, "gpt-5.6-luna", actor_id=None, changes={"enabled": False})
    await db.commit()

    await service.seed_policies(db)
    await service.seed_models(db)
    await db.commit()

    assert (await service.module_policies(db))["leave"].write_enabled is True
    assert (await service.get_model(db, "gpt-5.6-luna")).enabled is False


async def test_the_assistant_ships_switched_off(db, seeded) -> None:
    settings = await service.get_settings(db)
    assert settings.enabled is False
    assert settings.audience_mode == AudienceMode.ALLOW_LIST
    assert settings.confirm_writes_default is True


async def test_settings_are_a_single_row(db, seeded) -> None:
    first = await service.get_settings(db)
    await db.commit()
    assert (await service.get_settings(db)).id == first.id


async def test_an_unknown_model_cannot_be_selected(db, seeded) -> None:
    with pytest.raises(service.AssistantNotFoundError):
        await service.update_settings(db, actor_id=None, changes={"model_key": "gpt-9"})


async def test_a_disabled_model_cannot_be_selected(db, seeded) -> None:
    await service.update_model(db, "gpt-5.6-luna", actor_id=None, changes={"enabled": False})
    with pytest.raises(service.AssistantConflictError):
        await service.update_settings(db, actor_id=None, changes={"model_key": "gpt-5.6-luna"})


async def test_the_active_model_cannot_be_disabled(db, seeded) -> None:
    """Otherwise the next turn fails for everyone with nothing to point at."""
    active = (await service.get_settings(db)).model_key
    with pytest.raises(service.AssistantConflictError):
        await service.update_model(db, active, actor_id=None, changes={"enabled": False})


async def test_a_nonsense_effort_is_refused(db, seeded) -> None:
    with pytest.raises(service.AssistantError):
        await service.update_settings(db, actor_id=None, changes={"reasoning_effort": "wild"})


async def test_a_cost_cap_can_be_removed_by_setting_it_to_null(db, seeded) -> None:
    await service.update_settings(
        db, actor_id=None, changes={"daily_cost_cap_user_usd": Decimal("5")}
    )
    assert (await service.get_settings(db)).daily_cost_cap_user_usd == Decimal("5")
    await service.update_settings(db, actor_id=None, changes={"daily_cost_cap_user_usd": None})
    assert (await service.get_settings(db)).daily_cost_cap_user_usd is None


async def test_a_policy_for_an_unknown_module_is_refused(db, seeded) -> None:
    with pytest.raises(service.AssistantNotFoundError):
        await service.update_module_policy(db, "nonsense", actor_id=None, changes={})


async def test_a_policy_for_an_unknown_tool_is_refused(db, seeded) -> None:
    with pytest.raises(service.AssistantNotFoundError):
        await service.update_tool_policy(db, "leave.invent", actor_id=None, changes={})


async def test_a_rule_can_name_a_person_by_email(db, seeded) -> None:
    user = await _user(db, "amina@hamdaz.com")
    rule = await service.create_rule(
        db, actor_id=None, subject_type="user", subject="amina@hamdaz.com", effect="allow"
    )
    assert rule.subject_id == str(user.id)
    assert "amina@hamdaz.com" in rule.subject_label


async def test_a_rule_can_name_a_team_by_handle(db, seeded) -> None:
    team = await teams_service.create_team(db, name="Presales")
    await db.commit()
    rule = await service.create_rule(
        db, actor_id=None, subject_type="team", subject="presales", effect="allow"
    )
    assert rule.subject_id == str(team.id)


async def test_a_rule_can_name_a_role(db, seeded) -> None:
    rule = await service.create_rule(
        db, actor_id=None, subject_type="role", subject="manager", effect="block"
    )
    assert rule.subject_id == "manager"


async def test_a_rule_naming_nobody_is_refused(db, seeded) -> None:
    with pytest.raises(service.AssistantNotFoundError):
        await service.create_rule(
            db, actor_id=None, subject_type="user", subject="ghost@hamdaz.com", effect="allow"
        )


async def test_the_same_rule_cannot_be_added_twice(db, seeded) -> None:
    await _user(db, "amina@hamdaz.com")
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject="amina@hamdaz.com", effect="allow"
    )
    with pytest.raises(service.AssistantConflictError):
        await service.create_rule(
            db, actor_id=None, subject_type="user", subject="amina@hamdaz.com", effect="allow"
        )


async def test_the_opposite_rule_is_allowed_and_blocking_wins(db, seeded) -> None:
    """Keeping both is deliberate: a block laid over an allow is how somebody is
    suspended without losing the record that they were released."""
    user = await _user(db, "amina@hamdaz.com")
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject="amina@hamdaz.com", effect="allow"
    )
    await service.create_rule(
        db, actor_id=None, subject_type="user", subject="amina@hamdaz.com", effect="block"
    )
    await db.commit()
    actor = Actor(
        user_id=user.id,
        roles=frozenset(),
        team_ids=frozenset(),
        access_modules=frozenset(),
    )
    verdict = admit(_settings(), await service.list_rules(db), actor)
    assert verdict.admitted is False


async def test_the_rate_limit_counts_a_persons_recent_turns(db, seeded) -> None:
    user = await _user(db, "amina@hamdaz.com")
    settings = await service.update_settings(
        db, actor_id=None, changes={"turns_per_user_per_hour": 1}
    )
    conversation = await service.create_conversation(db, user=user)
    await service.create_run(
        db, conversation=conversation, user=user, settings=settings, user_text="hello"
    )
    await db.commit()
    verdict = await service.check_limits(db, settings, user.id)
    assert verdict.admitted is False
    assert verdict.code == "rate_limited"


async def test_a_blocked_turn_does_not_count_against_the_rate_limit(db, seeded) -> None:
    """A refusal is not usage; being refused must not then lock somebody out."""
    from app.models.assistant import RunStatus

    user = await _user(db, "amina@hamdaz.com")
    settings = await service.update_settings(
        db, actor_id=None, changes={"turns_per_user_per_hour": 1}
    )
    conversation = await service.create_conversation(db, user=user)
    await service.create_run(
        db,
        conversation=conversation,
        user=user,
        settings=settings,
        user_text="hello",
        status=RunStatus.BLOCKED,
    )
    await db.commit()
    assert (await service.check_limits(db, settings, user.id)).admitted is True


async def test_the_daily_cost_cap_stops_a_person(db, seeded) -> None:
    user = await _user(db, "amina@hamdaz.com")
    settings = await service.update_settings(
        db, actor_id=None, changes={"daily_cost_cap_user_usd": Decimal("0.10")}
    )
    conversation = await service.create_conversation(db, user=user)
    run = await service.create_run(
        db, conversation=conversation, user=user, settings=settings, user_text="hello"
    )
    run.cost_usd = Decimal("0.25")
    await db.commit()
    verdict = await service.check_limits(db, settings, user.id)
    assert verdict.admitted is False
    assert verdict.code == "cost_cap_user"


async def test_one_persons_spending_does_not_stop_another(db, seeded) -> None:
    spender = await _user(db, "amina@hamdaz.com")
    other = await _user(db, "bilal@hamdaz.com")
    settings = await service.update_settings(
        db, actor_id=None, changes={"daily_cost_cap_user_usd": Decimal("0.10")}
    )
    conversation = await service.create_conversation(db, user=spender)
    run = await service.create_run(
        db, conversation=conversation, user=spender, settings=settings, user_text="hello"
    )
    run.cost_usd = Decimal("0.25")
    await db.commit()
    assert (await service.check_limits(db, settings, other.id)).admitted is True


async def test_the_company_cap_stops_everybody(db, seeded) -> None:
    spender = await _user(db, "amina@hamdaz.com")
    other = await _user(db, "bilal@hamdaz.com")
    settings = await service.update_settings(
        db, actor_id=None, changes={"daily_cost_cap_total_usd": Decimal("0.10")}
    )
    conversation = await service.create_conversation(db, user=spender)
    run = await service.create_run(
        db, conversation=conversation, user=spender, settings=settings, user_text="hello"
    )
    run.cost_usd = Decimal("0.25")
    await db.commit()
    verdict = await service.check_limits(db, settings, other.id)
    assert verdict.admitted is False
    assert verdict.code == "cost_cap_total"


async def test_the_policy_matrix_reports_what_actually_applies(db, seeded) -> None:
    await service.update_module_policy(
        db, "leave", actor_id=None, changes={"write_enabled": True, "confirm_writes": False}
    )
    await service.update_tool_policy(
        db, "leave.reject", actor_id=None, changes={"confirm_override": True}
    )
    await db.commit()

    matrix = service.policy_matrix(
        await service.get_settings(db),
        await service.module_policies(db),
        await service.tool_policies(db),
    )
    leave = next(m for m in matrix if m["module_key"] == "leave")
    by_key = {t["tool_key"]: t for t in leave["tools"]}
    assert by_key["leave.request"]["effective_confirm"] is False
    assert by_key["leave.reject"]["effective_confirm"] is True
    assert by_key["leave.mine"]["effective_confirm"] is False  # a read never asks


async def test_a_conversation_belongs_to_one_person(db, seeded) -> None:
    owner = await _user(db, "amina@hamdaz.com")
    other = await _user(db, "bilal@hamdaz.com")
    conversation = await service.create_conversation(db, user=owner)
    await db.commit()
    with pytest.raises(service.AssistantNotFoundError):
        await service.get_conversation(db, conversation.id, user_id=other.id)


async def test_the_first_message_names_the_conversation(db, seeded) -> None:
    user = await _user(db, "amina@hamdaz.com")
    conversation = await service.create_conversation(db, user=user)
    await service.add_message(
        db, conversation, role="user", content="How much leave do I have left?", run_id=None
    )
    await db.commit()
    assert conversation.title == "How much leave do I have left?"


async def test_messages_are_numbered_in_order(db, seeded) -> None:
    user = await _user(db, "amina@hamdaz.com")
    conversation = await service.create_conversation(db, user=user)
    for text in ("one", "two", "three"):
        await service.add_message(db, conversation, role="user", content=text, run_id=None)
    await db.commit()
    messages = await service.list_messages(db, conversation.id)
    assert [m.seq for m in messages] == [1, 2, 3]
    assert [m.content for m in messages] == ["one", "two", "three"]
