"""The assistant's rules, decided without a database, HTTP or a model.

Who may talk to the assistant and which tools they are shown are pure functions
over already-loaded rows, so every case here runs in milliseconds. This is the
half that says no, which is the half worth testing hardest.
"""


from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.assistant.agent import tool_payload
from app.assistant.catalogue import (
    DEFAULT_MODEL,
    EVERYDAY_TOOLS,
    GROUPS,
    LIVE_TOOLS,
    MODELS,
    TOOLS,
    TOOLS_BY_KEY,
    TOOLS_BY_NAME,
    Param,
    ToolSpec,
)
from app.assistant.executor import ToolExecutor
from app.assistant.policy import (
    Actor,
    ResolvedTool,
    admit,
    cost_of,
    effective_confirm,
    effective_roles,
    resolve_tools,
    tool_enabled,
)
from app.models.assistant import (
    AssistantAccessRule,
    AssistantModulePolicy,
    AssistantSettings,
    AssistantToolPolicy,
    AudienceMode,
    RuleEffect,
    SubjectType,
)

# ── fixtures for the pure half ─────────────────────────────────────────


def _settings(**kw) -> AssistantSettings:
    row = AssistantSettings(id=1)
    row.enabled = kw.get("enabled", True)
    row.audience_mode = kw.get("audience_mode", AudienceMode.EVERYONE)
    row.confirm_writes_default = kw.get("confirm_writes_default", True)
    row.model_key = kw.get("model_key", "gpt-5.6-sol")
    return row


def _rule(subject_type: str, subject_id: str, effect: str, *, enabled: bool = True):
    return AssistantAccessRule(
        subject_type=subject_type,
        subject_id=subject_id,
        subject_label=subject_id,
        effect=effect,
        enabled=enabled,
    )


def _actor(**kw) -> Actor:
    return Actor(
        user_id=kw.get("user_id", uuid.uuid4()),
        roles=frozenset(kw.get("roles", ())),
        team_ids=frozenset(kw.get("team_ids", ())),
        access_modules=frozenset(kw.get("access_modules", ())),
    )


def _module(key: str, **kw) -> AssistantModulePolicy:
    row = AssistantModulePolicy(module_key=key)
    row.read_enabled = kw.get("read_enabled", True)
    row.write_enabled = kw.get("write_enabled", False)
    row.confirm_writes = kw.get("confirm_writes")
    row.allowed_roles = kw.get("allowed_roles")
    return row


def _tool(key: str, module_key: str, **kw) -> AssistantToolPolicy:
    row = AssistantToolPolicy(tool_key=key, module_key=module_key)
    row.enabled = kw.get("enabled", True)
    row.confirm_override = kw.get("confirm_override")
    row.allowed_roles = kw.get("allowed_roles")
    return row


# ── the catalogue ──────────────────────────────────────────────────────


def test_every_tool_has_a_unique_key_and_function_name() -> None:
    keys = [t.key for t in TOOLS]
    names = [t.name for t in TOOLS]
    assert len(set(keys)) == len(keys)
    assert len(set(names)) == len(names)


def test_function_names_are_valid_identifiers() -> None:
    # OpenAI rejects a tool name with a dot in it, which is why keys are mapped.
    for spec in TOOLS:
        assert spec.name.replace("__", "_").isidentifier(), spec.name


def test_get_tools_are_reads_and_everything_else_is_a_write() -> None:
    # The kind decides whether an action can happen without being confirmed, so
    # a GET marked write is merely annoying but a POST marked read is a hole.
    for spec in TOOLS:
        assert spec.is_write == (spec.method != "GET"), spec.key


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_schemas_satisfy_strict_mode(spec: ToolSpec) -> None:
    """Strict mode needs every property required and no extras, or the API 400s."""
    schema = spec.schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_optional_parameters_are_nullable(spec: ToolSpec) -> None:
    schema = spec.schema()
    for param in spec.params:
        prop = schema["properties"][param.name]
        types = prop.get("type", [])
        nullable = "null" in types or any(
            b.get("type") == "null" for b in prop.get("anyOf", [])
        )
        assert nullable is (not param.required), f"{spec.key}.{param.name}"


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_every_path_placeholder_has_a_path_parameter(spec: ToolSpec) -> None:
    import re

    placeholders = set(re.findall(r"\{(\w+)\}", spec.path))
    supplied = {p.name for p in spec.params if p.location == "path"}
    assert placeholders == supplied, spec.key


def test_model_prices_are_positive() -> None:
    for model in MODELS:
        assert model.input_price > 0 and model.output_price > 0
        # Cached input is a discount, never a premium.
        assert model.cached_input_price <= model.input_price


# ── admission ──────────────────────────────────────────────────────────


def test_nobody_gets_in_while_the_master_switch_is_off() -> None:
    actor = _actor(roles=["super_admin"])
    verdict = admit(_settings(enabled=False), [], actor)
    assert verdict.admitted is False
    assert verdict.code == "disabled"


def test_everyone_mode_admits_an_ordinary_person() -> None:
    assert admit(_settings(), [], _actor()).admitted is True


def test_allow_list_mode_refuses_without_a_rule() -> None:
    verdict = admit(_settings(audience_mode=AudienceMode.ALLOW_LIST), [], _actor())
    assert verdict.admitted is False
    assert verdict.code == "not_released"


def test_allow_list_admits_a_named_person() -> None:
    actor = _actor()
    rules = [_rule(SubjectType.USER, str(actor.user_id), RuleEffect.ALLOW)]
    assert admit(_settings(audience_mode=AudienceMode.ALLOW_LIST), rules, actor).admitted


def test_allow_list_admits_through_a_team() -> None:
    team = uuid.uuid4()
    actor = _actor(team_ids=[team])
    rules = [_rule(SubjectType.TEAM, str(team), RuleEffect.ALLOW)]
    assert admit(_settings(audience_mode=AudienceMode.ALLOW_LIST), rules, actor).admitted


def test_allow_list_admits_through_a_role() -> None:
    actor = _actor(roles=["manager"])
    rules = [_rule(SubjectType.ROLE, "manager", RuleEffect.ALLOW)]
    assert admit(_settings(audience_mode=AudienceMode.ALLOW_LIST), rules, actor).admitted


def test_a_block_beats_an_allow() -> None:
    """The rule people are most surprised by, and the one that must not slip."""
    team = uuid.uuid4()
    actor = _actor(team_ids=[team])
    rules = [
        _rule(SubjectType.USER, str(actor.user_id), RuleEffect.BLOCK),
        _rule(SubjectType.TEAM, str(team), RuleEffect.ALLOW),
    ]
    verdict = admit(_settings(), rules, actor)
    assert verdict.admitted is False
    assert verdict.code == "blocked"


def test_a_disabled_rule_does_nothing() -> None:
    actor = _actor()
    rules = [_rule(SubjectType.USER, str(actor.user_id), RuleEffect.BLOCK, enabled=False)]
    # admit() is given only enabled rules by the caller, but a disabled one
    # reaching it must still be ignored rather than trusted.
    assert admit(_settings(), rules, actor).admitted is True


def test_a_super_admin_is_never_blocked_while_the_switch_is_on() -> None:
    # They are the person configuring it; trying it before release is the point.
    actor = _actor(roles=["super_admin"])
    rules = [_rule(SubjectType.USER, str(actor.user_id), RuleEffect.BLOCK)]
    assert admit(_settings(audience_mode=AudienceMode.ALLOW_LIST), rules, actor).admitted


# ── which tools a person is shown ──────────────────────────────────────


def _resolve(actor: Actor, *, settings=None, modules=None, tools=None):
    return resolve_tools(settings or _settings(), modules or {}, tools or {}, actor)


def test_open_modules_reach_everyone() -> None:
    keys = {t.key for t in _resolve(_actor())}
    assert "leave.mine" in keys
    assert "me.roles" in keys


def test_no_writes_until_a_super_admin_enables_the_module() -> None:
    """The default that matters: reading is recoverable, writing is not."""
    assert not [t for t in _resolve(_actor()) if t.spec.is_write]


def test_enabling_writes_reveals_them() -> None:
    tools = _resolve(_actor(), modules={"leave": _module("leave", write_enabled=True)})
    assert "leave.request" in {t.key for t in tools}


def test_disabling_reads_hides_a_whole_module() -> None:
    tools = _resolve(_actor(), modules={"leave": _module("leave", read_enabled=False)})
    assert not [t for t in tools if t.spec.module_key == "leave"]


def test_an_access_gated_module_needs_the_grant() -> None:
    assert "teams.list" not in {t.key for t in _resolve(_actor())}
    with_access = _resolve(_actor(access_modules=["teams"]))
    assert "teams.list" in {t.key for t in with_access}


def test_an_admin_gated_module_needs_an_admin_role() -> None:
    assert "roles.assignments" not in {t.key for t in _resolve(_actor())}
    admin = _resolve(_actor(roles=["manager"]))
    assert "roles.assignments" in {t.key for t in admin}


def test_a_disabled_tool_disappears_even_though_its_module_is_on() -> None:
    tools = _resolve(_actor(), tools={"leave.mine": _tool("leave.mine", "leave", enabled=False)})
    keys = {t.key for t in tools}
    assert "leave.mine" not in keys
    assert "leave.calendar" in keys


def test_a_role_restriction_hides_a_module_from_others() -> None:
    modules = {"leave": _module("leave", allowed_roles=["ceo"])}
    assert not [t for t in _resolve(_actor(), modules=modules) if t.spec.module_key == "leave"]
    allowed = _resolve(_actor(roles=["ceo"]), modules=modules)
    assert [t for t in allowed if t.spec.module_key == "leave"]


def test_a_tool_role_restriction_overrides_the_modules() -> None:
    modules = {"leave": _module("leave", allowed_roles=["ceo"])}
    tools = {"leave.mine": _tool("leave.mine", "leave", allowed_roles=["member"])}
    keys = {t.key for t in _resolve(_actor(roles=["member"]), modules=modules, tools=tools)}
    assert "leave.mine" in keys
    # The module's own restriction still governs everything else in it.
    assert "leave.calendar" not in keys


# ── confirmation ───────────────────────────────────────────────────────


def test_confirmation_falls_back_to_the_global_default() -> None:
    assert effective_confirm(_settings(confirm_writes_default=True), None, None) is True
    assert effective_confirm(_settings(confirm_writes_default=False), None, None) is False


def test_a_module_overrides_the_global_default() -> None:
    module = _module("leave", confirm_writes=False)
    assert effective_confirm(_settings(confirm_writes_default=True), module, None) is False


def test_a_tool_overrides_its_module() -> None:
    module = _module("leave", confirm_writes=False)
    tool = _tool("leave.reject", "leave", confirm_override=True)
    assert effective_confirm(_settings(), module, tool) is True


def test_reads_never_ask_for_confirmation() -> None:
    tools = _resolve(
        _actor(),
        settings=_settings(confirm_writes_default=True),
        modules={"leave": _module("leave", write_enabled=True)},
    )
    for tool in tools:
        assert tool.requires_confirmation is tool.spec.is_write


def test_writes_can_be_turned_loose_per_module() -> None:
    tools = _resolve(
        _actor(),
        modules={"leave": _module("leave", write_enabled=True, confirm_writes=False)},
    )
    request = next(t for t in tools if t.key == "leave.request")
    assert request.requires_confirmation is False


def test_effective_roles_prefers_the_tool() -> None:
    module = _module("leave", allowed_roles=["ceo"])
    tool = _tool("leave.mine", "leave", allowed_roles=["member"])
    assert effective_roles(module, tool) == ["member"]
    assert effective_roles(module, None) == ["ceo"]
    assert effective_roles(None, None) is None


def test_a_write_is_refused_when_no_module_policy_exists_yet() -> None:
    """Before the seed runs there are no rows; a write must not default to on."""
    write = TOOLS_BY_KEY["leave.request"]
    read = TOOLS_BY_KEY["leave.mine"]
    assert tool_enabled(write, None, None) is False
    assert tool_enabled(read, None, None) is True


# ── cost ───────────────────────────────────────────────────────────────


def test_cost_prices_uncached_and_cached_input_separately() -> None:
    from app.models.assistant import AssistantModel

    model = AssistantModel(
        key="m",
        name="m",
        description="",
        input_price=Decimal("4.00"),
        cached_input_price=Decimal("0.40"),
        output_price=Decimal("20.00"),
    )
    # 1M input of which 500k cached, 100k output:
    #   500k * $4 + 500k * $0.40 + 100k * $20  per million
    cost = cost_of(model, input_tokens=1_000_000, cached_tokens=500_000, output_tokens=100_000)
    assert cost == Decimal("4.200000")


def test_cost_is_zero_for_a_turn_that_used_nothing() -> None:
    from app.models.assistant import AssistantModel

    model = AssistantModel(
        key="m", name="m", description="",
        input_price=Decimal("4"), cached_input_price=Decimal("0.4"), output_price=Decimal("20"),
    )
    assert cost_of(model, input_tokens=0, cached_tokens=0, output_tokens=0) == Decimal(0)


# ── building the request behind a tool call ────────────────────────────


@pytest.fixture
def executor() -> ToolExecutor:
    return ToolExecutor(None, api_prefix="/api/v1", cookie_name="hamdaz_session")


def test_a_path_parameter_is_substituted(executor: ToolExecutor) -> None:
    url, query, body = executor.build(
        TOOLS_BY_KEY["teams.get"], {"ref": "presales"}
    )
    assert url == "/api/v1/teams/presales"
    assert query == {} and body is None


def test_a_path_parameter_is_url_encoded(executor: ToolExecutor) -> None:
    url, _, _ = executor.build(TOOLS_BY_KEY["quotes.by_number"], {"number": "QT/000 1"})
    assert url == "/api/v1/quotes/by-number/QT%2F000%201"


def test_nulls_are_dropped_from_the_query(executor: ToolExecutor) -> None:
    _, query, _ = executor.build(
        TOOLS_BY_KEY["leave.calendar"], {"start": None, "days": 14}
    )
    assert query == {"days": "14"}


def test_booleans_reach_the_query_as_words(executor: ToolExecutor) -> None:
    _, query, _ = executor.build(
        TOOLS_BY_KEY["proposals.my_tasks"], {"open_only": False, "limit": None}
    )
    assert query == {"open_only": "false"}


def test_a_body_is_built_for_a_write(executor: ToolExecutor) -> None:
    url, query, body = executor.build(
        TOOLS_BY_KEY["leave.request"],
        {
            "leave_type": "annual",
            "start_date": "2026-10-01",
            "end_date": "2026-10-03",
            "reason": None,
        },
    )
    assert url == "/api/v1/leave/requests"
    assert body == {"leave_type": "annual", "start_date": "2026-10-01", "end_date": "2026-10-03"}
    assert query == {}


def test_a_missing_path_parameter_is_refused(executor: ToolExecutor) -> None:
    with pytest.raises(ValueError, match="required"):
        executor.build(TOOLS_BY_KEY["teams.get"], {"ref": None})


# ── the two tool dialects ──────────────────────────────────────────────
#
# The Responses API and the Realtime API do not accept the same tool object,
# and the difference is not documented anywhere a reader would look. Realtime
# rejects `strict` outright as an unknown parameter rather than ignoring it, so
# sending the Responses shape fails the whole session with a 400. Every unit
# test passed while that was broken, because they all stub the model — it only
# showed up against the real API. Hence these.


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_the_responses_shape_is_strict(spec: ToolSpec) -> None:
    """Strict is what makes arguments guaranteed to validate. Keep it."""
    assert spec.definition()["strict"] is True


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_the_realtime_shape_has_no_strict(spec: ToolSpec) -> None:
    """Realtime 400s on it, which takes the whole spoken session down."""
    assert "strict" not in spec.realtime_definition()


#: Everything the Realtime API accepts on a function tool. Anything else is
#: rejected as an unknown parameter and fails the whole spoken session.
REALTIME_FIELDS = {"type", "name", "description", "parameters"}


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_the_realtime_shape_carries_nothing_realtime_rejects(spec: ToolSpec) -> None:
    """Written as a whitelist on purpose.

    The version of this test that compared the two shapes to each other passed
    while `defer_loading` was breaking every spoken session, because the field
    was present in both. Asking what the realtime shape *contains* catches the
    next field too, whatever it turns out to be called.
    """
    assert set(spec.realtime_definition()) <= REALTIME_FIELDS


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_the_realtime_shape_keeps_what_the_model_needs(spec: ToolSpec) -> None:
    realtime = spec.realtime_definition()
    assert realtime["name"] == spec.name
    assert realtime["description"] == spec.description
    assert realtime["parameters"] == spec.schema()


def test_asking_for_the_realtime_shape_does_not_damage_the_other() -> None:
    """It is built by copying and popping, which is easy to get wrong."""
    spec = TOOLS[0]
    spec.realtime_definition()
    assert spec.definition()["strict"] is True


# ── the catalogue's own invariants ─────────────────────────────────────
#
# These exist because each of them has already been got wrong once. A tool
# built with `(one_param)` instead of `(one_param,)` is a Param rather than a
# tuple, and every schema call on it fails at runtime rather than at import.


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_params_are_a_tuple(spec: ToolSpec) -> None:
    """A missing trailing comma makes this a bare Param and breaks the schema."""
    assert isinstance(spec.params, tuple)
    assert all(isinstance(p, Param) for p in spec.params)


def test_every_everyday_tool_exists() -> None:
    """A typo here silently defers a tool people use constantly."""
    assert {spec.key for spec in LIVE_TOOLS} >= EVERYDAY_TOOLS


def test_the_everyday_set_stays_small() -> None:
    """It is the prompt on every turn. Growth here is paid for on every 'hi'."""
    assert len(EVERYDAY_TOOLS) <= 30


def test_everyday_tools_are_loaded_and_the_rest_deferred() -> None:
    for spec in LIVE_TOOLS:
        assert spec.deferred is (spec.key not in EVERYDAY_TOOLS), spec.key


def test_planned_tools_are_never_live() -> None:
    """The whole point of the distinction: a roadmap entry must be unreachable."""
    planned = [spec for spec in TOOLS if spec.status == "planned"]
    assert planned, "the roadmap should not be empty while there is work outstanding"
    for spec in planned:
        assert spec not in LIVE_TOOLS
        assert spec.name not in TOOLS_BY_NAME


def test_a_planned_tool_cannot_be_resolved_for_anybody() -> None:
    """Even a super admin with every module enabled must not be offered one."""
    actor = _actor(roles=["super_admin"], access_modules={g.key for g in GROUPS})
    modules = {g.key: _module(g.key, write_enabled=True) for g in GROUPS}
    offered = {t.key for t in _resolve(actor, modules=modules)}
    for spec in TOOLS:
        if spec.status == "planned":
            assert spec.key not in offered


@pytest.mark.parametrize("spec", TOOLS, ids=lambda s: s.key)
def test_every_tool_belongs_to_a_real_group(spec: ToolSpec) -> None:
    assert spec.module_key in {group.key for group in GROUPS}


def test_the_search_tool_rides_along_only_when_something_is_deferred() -> None:
    everyday = [ResolvedTool(s, False) for s in LIVE_TOOLS if not s.deferred]
    assert not any(t.get("type") == "tool_search" for t in tool_payload(everyday))

    mixed = [ResolvedTool(s, False) for s in LIVE_TOOLS]
    assert any(t.get("type") == "tool_search" for t in tool_payload(mixed))


def test_access_gated_groups_name_a_real_erp_module() -> None:
    """An 'access' gate is checked against the person's granted ERP modules.

    A group key that is not one of those can never be visible, so its tools
    reach nobody — silently, because nothing errors. That is how the work
    analytics tools shipped unreachable the first time.
    """
    from app.access.catalogue import MODULES

    erp = {module.key for module in MODULES}
    for group in GROUPS:
        if group.gate == "access":
            assert group.key in erp, f"{group.key} is gated on a module that does not exist"


# ── what each model can actually do ────────────────────────────────────
#
# Both of these flags exist because sending the wrong thing does not degrade,
# it fails the whole turn with a 400. The GPT-4 family rejects `reasoning`; the
# nano models reject the tool-search tool. Neither ignores it.


def test_a_model_that_cannot_search_is_sent_everything_loaded() -> None:
    tools = [ResolvedTool(spec, False) for spec in LIVE_TOOLS]
    payload = tool_payload(tools, "gpt-4o-mini")
    assert not any(entry.get("type") == "tool_search" for entry in payload)
    assert not any(entry.get("defer_loading") for entry in payload)
    # Nothing is hidden from it — it simply pays for the whole catalogue.
    assert len([e for e in payload if e.get("type") == "function"]) == len(tools)


def test_a_model_that_can_search_gets_the_deferred_shape() -> None:
    tools = [ResolvedTool(spec, False) for spec in LIVE_TOOLS]
    payload = tool_payload(tools, "gpt-5.6-terra")
    assert any(entry.get("type") == "tool_search" for entry in payload)
    assert any(entry.get("defer_loading") for entry in payload)


def test_an_unknown_model_is_assumed_current() -> None:
    """A key in the settings but not the catalogue is likelier new than old,
    and being wrong that way is a clear 400 rather than silent waste."""
    tools = [ResolvedTool(spec, False) for spec in LIVE_TOOLS]
    assert any(e.get("type") == "tool_search" for e in tool_payload(tools, "gpt-9-unheard-of"))


def test_the_catalogue_records_the_families_that_do_not_reason() -> None:
    by_key = {model.key: model for model in MODELS}
    for key in ("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"):
        assert by_key[key].supports_reasoning is False, key
        assert by_key[key].supports_tool_search is False, key


def test_the_current_family_can_do_both() -> None:
    by_key = {model.key: model for model in MODELS}
    for key in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"):
        assert by_key[key].supports_reasoning is True, key
        assert by_key[key].supports_tool_search is True, key


def test_the_default_model_can_search() -> None:
    """Otherwise a fresh install pays for the whole catalogue on every turn."""
    by_key = {model.key: model for model in MODELS}
    assert by_key[DEFAULT_MODEL].supports_tool_search is True
