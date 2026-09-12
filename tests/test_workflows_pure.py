"""The workflow module's rules, decided without a database or a model.

Templating, step validation, the shipped flow, the block catalogue the
builder renders, and the timeline projection are all pure functions over
dicts, so every case here runs in milliseconds.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.assistant.catalogue import TOOLS_BY_KEY
from app.models.workflow import RunEventKind, RunStatus
from app.workflows import catalogue, templating
from app.workflows.catalogue import (
    BLOCKS,
    BLOCKS_BY_KIND,
    PRESALES_KEY,
    PRESALES_STEPS,
    SCHEMAS,
    StepError,
    tool_choices,
    validate_steps,
)
from app.workflows.service import step_states
from app.workflows.templating import (
    TemplateError,
    condition_holds,
    lookup,
    render,
    render_value,
    truthy,
)

# ── templating: lookup ─────────────────────────────────────────────────

CONTEXT = {
    "task": {"title": "Pump spares", "id": "42"},
    "suppliers": {
        "verified": [
            {"name": "Acme", "email": "sales@acme.ae"},
            {"name": "Bolt", "email": ""},
        ]
    },
    "items": [
        {"description": "Gate valve", "part_number": "GV-100", "quantity": 2, "unit": "pcs"},
        {"description": "Gasket", "quantity": 10},
    ],
    "empty": [],
    "flag": False,
    "count": 0,
    "nothing": None,
    "docs": {"found": True},
}


def test_lookup_walks_dotted_paths_and_indexes() -> None:
    assert lookup(CONTEXT, "task.title") == "Pump spares"
    assert lookup(CONTEXT, "suppliers.verified[0].email") == "sales@acme.ae"
    assert lookup(CONTEXT, "suppliers.verified[1].name") == "Bolt"
    # A bare numeric segment indexes a list too.
    assert lookup(CONTEXT, "suppliers.verified.0.name") == "Acme"
    assert lookup(CONTEXT, "items[1].quantity") == 10


def test_lookup_is_forgiving_about_what_is_not_there() -> None:
    assert lookup(CONTEXT, "task.nope") is None
    assert lookup(CONTEXT, "task.nope", "dflt") == "dflt"
    assert lookup(CONTEXT, "suppliers.verified[9].name", "x") == "x"
    assert lookup(CONTEXT, "task.title.deeper", "x") == "x"
    assert lookup(CONTEXT, "nothing.deeper", "x") == "x"
    assert lookup(CONTEXT, "", "x") == CONTEXT  # nothing to walk: the context itself
    # A present falsy value is still the value, not the default.
    assert lookup(CONTEXT, "flag", "x") is False
    assert lookup(CONTEXT, "count", "x") == 0


# ── templating: render and the filters ─────────────────────────────────


def test_render_replaces_placeholders_as_text() -> None:
    assert render("Task: {{ task.title }} ({{task.id}})", CONTEXT) == "Task: Pump spares (42)"
    assert render("Missing: [{{ task.nope }}]", CONTEXT) == "Missing: []"
    assert render("", CONTEXT) == ""
    # A dict or list with no filter is rendered as JSON rather than Python repr.
    assert render("{{ docs }}", CONTEXT) == '{"found": true}'


def test_render_applies_each_filter() -> None:
    assert render("{{ task.title | upper }}", CONTEXT) == "PUMP SPARES"
    assert render("{{ items | count }}", CONTEXT) == "2"
    assert render("{{ empty | count }}", CONTEXT) == "0"
    assert render("{{ nothing | count }}", CONTEXT) == "0"
    assert render("{{ task.title | text }}", CONTEXT) == "Pump spares"
    assert render("{{ docs | json }}", CONTEXT) == '{\n  "found": true\n}'

    bullets = render("{{ items | bullets }}", CONTEXT)
    assert bullets == "- Gate valve, part_number: GV-100, quantity: 2, unit: pcs\n- Gasket, quantity: 10"
    lines = render("{{ items | lines }}", CONTEXT)
    assert lines == "Gate valve, part_number: GV-100, quantity: 2, unit: pcs\nGasket, quantity: 10"
    # bullets over something that is not a list falls back to text.
    assert render("{{ task.title | bullets }}", CONTEXT) == "Pump spares"
    assert render("{{ suppliers.verified | bullets }}", CONTEXT) == "- Acme\n- Bolt"


def test_render_refuses_an_unknown_filter() -> None:
    with pytest.raises(TemplateError):
        render("{{ task.title | shout }}", CONTEXT)


def test_render_value_keeps_a_whole_placeholder_s_type() -> None:
    assert render_value("{{ items }}", CONTEXT) is CONTEXT["items"]
    assert render_value("  {{ docs.found }} ", CONTEXT) is True
    assert render_value("{{ count }}", CONTEXT) == 0
    assert render_value("{{ task.nope }}", CONTEXT) is None
    # Text around it, or a filter, makes it a string again.
    assert render_value("x {{ count }}", CONTEXT) == "x 0"
    assert render_value("{{ items | count }}", CONTEXT) == "2"
    # Nested structures are walked; other scalars pass through untouched.
    out = render_value(
        {"team": "{{ task.id }}", "items": "{{ items }}", "n": 3, "list": ["{{ task.title }}", True]},
        CONTEXT,
    )
    assert out == {"team": "42", "items": CONTEXT["items"], "n": 3, "list": ["Pump spares", True]}


# ── templating: conditions ─────────────────────────────────────────────


def test_truthy_reads_words_and_collections_like_a_person() -> None:
    assert truthy("yes") and truthy("1") and truthy([1]) and truthy({"a": 1})
    for falsy in ("", "0", "false", "No", " none ", "null", [], {}, 0, None, False):
        assert not truthy(falsy), falsy


def test_condition_holds_with_each_kind_of_is() -> None:
    assert condition_holds(None, CONTEXT)
    assert condition_holds({}, CONTEXT)
    assert condition_holds({"path": ""}, CONTEXT)
    # Boolean: truthiness of the value.
    assert condition_holds({"path": "docs.found", "is": True}, CONTEXT)
    assert not condition_holds({"path": "docs.found", "is": False}, CONTEXT)
    assert condition_holds({"path": "empty", "is": False}, CONTEXT)
    assert condition_holds({"path": "task.nope", "is": False}, CONTEXT)
    # No ``is`` means "is truthy".
    assert condition_holds({"path": "items"}, CONTEXT)
    assert not condition_holds({"path": "empty"}, CONTEXT)
    # None: the value must be absent.
    assert condition_holds({"path": "nothing", "is": None}, CONTEXT)
    assert condition_holds({"path": "task.nope", "is": None}, CONTEXT)
    assert not condition_holds({"path": "count", "is": None}, CONTEXT)
    # A list: membership, compared as text when types differ.
    assert condition_holds({"path": "task.id", "is": ["41", "42"]}, CONTEXT)
    assert condition_holds({"path": "task.id", "is": [41, 42]}, CONTEXT)
    assert not condition_holds({"path": "task.id", "is": ["7"]}, CONTEXT)
    # A string: equality as text.
    assert condition_holds({"path": "task.title", "is": "Pump spares"}, CONTEXT)
    assert condition_holds({"path": "count", "is": "0"}, CONTEXT)
    assert not condition_holds({"path": "task.title", "is": "Other"}, CONTEXT)


# ── the catalogue: validate_steps ──────────────────────────────────────


def _notify(key: str = "n") -> dict:
    return {"key": key, "kind": "notify", "config": {"title": "Hi"}}


def test_validate_steps_rejects_duplicate_keys() -> None:
    with pytest.raises(StepError, match="Two steps are called 'n'"):
        validate_steps([_notify("n"), _notify("n")])


def test_validate_steps_rejects_an_unknown_kind() -> None:
    with pytest.raises(StepError, match="no block called 'teleport'"):
        validate_steps([{"key": "t", "kind": "teleport", "config": {}}])


def test_validate_steps_rejects_a_missing_required_field() -> None:
    with pytest.raises(StepError, match="Step 'ask': Title is required"):
        validate_steps([{"key": "ask", "kind": "ask_user", "config": {"message": "?"}}])
    with pytest.raises(StepError, match="Step 'e': Recipients from is required"):
        validate_steps([{"key": "e", "kind": "email", "config": {"subject": "s", "body": "b"}}])


def test_validate_steps_rejects_an_endpoint_naming_an_unknown_tool() -> None:
    with pytest.raises(StepError, match="'nope.nothing' is not a route the app has"):
        validate_steps([{"key": "c", "kind": "endpoint", "config": {"tool": "nope.nothing"}}])
    # A client-side tool is not a route either.
    with pytest.raises(StepError, match="is not a route the app has"):
        validate_steps([{"key": "w", "kind": "wait_status", "config": {"tool": "app.open", "until": "x"}}])


def test_validate_steps_rejects_bad_shapes() -> None:
    with pytest.raises(StepError, match="at least one step"):
        validate_steps([])
    with pytest.raises(StepError, match="at least one step"):
        validate_steps("not a list")  # type: ignore[arg-type]
    with pytest.raises(StepError, match="Step 1 is not an object"):
        validate_steps(["notify"])  # type: ignore[list-item]
    with pytest.raises(StepError, match="needs a short key"):
        validate_steps([{"key": "bad key!", "kind": "notify", "config": {"title": "x"}}])
    with pytest.raises(StepError, match="config must be an object"):
        validate_steps([{"key": "n", "kind": "notify", "config": "title"}])
    with pytest.raises(StepError, match="when must be an object"):
        validate_steps([{**_notify(), "when": "docs.found"}])


def test_validate_steps_normalises() -> None:
    out = validate_steps(
        [
            {"key": " docs ", "kind": "documents"},
            {"key": "n", "kind": "notify", "name": "Say hi", "config": {"title": "Hi"}, "when": {"path": "x"}},
        ]
    )
    assert out == [
        {"key": "docs", "kind": "documents", "name": "Read the task's documents", "config": {}, "when": None},
        {"key": "n", "kind": "notify", "name": "Say hi", "config": {"title": "Hi"}, "when": {"path": "x"}},
    ]
    assert set(out[0]) == {"key", "kind", "name", "config", "when"}


def test_the_shipped_presales_flow_validates() -> None:
    out = validate_steps(PRESALES_STEPS)
    assert [s["key"] for s in out] == [s["key"] for s in PRESALES_STEPS]
    assert len({s["key"] for s in out}) == len(out)
    assert catalogue.FLOWS[0].key == PRESALES_KEY
    assert catalogue.FLOWS[0].steps is PRESALES_STEPS
    # The ask-for-documents step is the one gated on what the documents step found.
    ask = next(s for s in out if s["key"] == "ask_docs")
    assert ask["when"] == {"path": "docs.found", "is": False}
    assert ask["config"]["allow_files"] is True


def test_the_presales_flow_s_routes_exist_in_the_assistant_catalogue() -> None:
    choices = {t["key"] for t in tool_choices()}
    for step in PRESALES_STEPS:
        if step["kind"] in ("endpoint", "wait_status"):
            tool = step["config"]["tool"]
            assert tool in choices, tool
            spec = TOOLS_BY_KEY[tool]
            assert spec.is_live and not spec.is_client
    # The picker never offers a browser-side tool or the app's own meta tools.
    assert all(not TOOLS_BY_KEY[t["key"]].is_client for t in tool_choices())
    assert all(t["module"] != "app" for t in tool_choices())


# ── the catalogue: what the builder is shown ───────────────────────────

BUILDER_FIELD_TYPES = {"text", "textarea", "number", "boolean", "select", "json", "tool", "path", "fields"}


def test_every_block_field_is_a_type_the_builder_knows() -> None:
    assert len(BLOCKS) == 12
    for block in BLOCKS:
        for f in block.fields:
            assert f.type in BUILDER_FIELD_TYPES, f"{block.kind}.{f.key}: {f.type}"
            if f.type == "select":
                assert f.options, f"{block.kind}.{f.key} has no options"
                if f.default not in (None, ""):
                    assert f.default in f.options
        schema = block.config_schema()
        assert [s["key"] for s in schema] == [f.key for f in block.fields]
        assert all(set(s) == {"key", "label", "type", "required", "help", "options", "default"} for s in schema)
    assert BLOCKS_BY_KIND["ask_user"].waits == "user"
    assert BLOCKS_BY_KIND["wait_email"].waits == "event"
    assert BLOCKS_BY_KIND["email"].switch == "send_email"
    assert BLOCKS_BY_KIND["zoho_create"].switch == "write_zoho"
    assert BLOCKS_BY_KIND["sharepoint_attach"].switch == "write_sharepoint"


def _walk_strict(schema: dict, where: str) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, where
        props = schema.get("properties") or {}
        assert set(schema.get("required") or []) == set(props), where
        for key, sub in props.items():
            _walk_strict(sub, f"{where}.{key}")
    if schema.get("type") == "array":
        _walk_strict(schema["items"], f"{where}[]")


def test_schemas_satisfy_strict_mode() -> None:
    assert set(SCHEMAS) == {"requirements", "suppliers"}
    for name, schema in SCHEMAS.items():
        _walk_strict(schema, name)


# ── the timeline projection ────────────────────────────────────────────


def _event(kind: str, step_key: str | None, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, step_key=step_key, payload=payload)


def _run(status: str, step_index: int, events: list, context: dict) -> SimpleNamespace:
    steps = [
        {"key": "docs", "kind": "documents", "name": "Read"},
        {"key": "ask_docs", "kind": "ask_user", "name": "Ask", "when": {"path": "docs.found", "is": False}},
        {"key": "extract", "kind": "extract", "name": "Extract"},
        {"key": "later", "kind": "notify", "name": "Later", "when": {"path": "mode", "is": "loud"}},
        {"key": "done", "kind": "notify", "name": "Done"},
    ]
    return SimpleNamespace(steps=steps, step_index=step_index, status=status, events=events, context=context)


def test_step_states_reports_done_skipped_waiting_and_foretells_a_skip() -> None:
    run = _run(
        RunStatus.WAITING_USER,
        2,
        [
            _event(RunEventKind.STEP_COMPLETED, "docs", {"note": "1 file(s) on the task"}),
            _event(RunEventKind.STEP_SKIPPED, "ask_docs"),
            _event(RunEventKind.WAITING, "extract", {"note": "thinking"}),
        ],
        {"docs": {"found": True}, "mode": "quiet"},
    )
    states = {s["key"]: s for s in step_states(run)}
    assert [s["key"] for s in step_states(run)] == ["docs", "ask_docs", "extract", "later", "done"]
    assert states["docs"]["state"] == "done" and states["docs"]["note"] == "1 file(s) on the task"
    assert states["ask_docs"]["state"] == "skipped"
    assert states["extract"]["state"] == "waiting" and states["extract"]["note"] == "thinking"
    # ``later`` has not been reached, but its condition already reads false.
    assert states["later"]["state"] == "skipped"
    assert states["done"]["state"] == "pending"
    assert states["done"]["kind"] == "notify" and states["done"]["name"] == "Done"


def test_step_states_on_a_running_failed_and_finished_run() -> None:
    assert step_states(_run(RunStatus.RUNNING, 0, [], {}))[0]["state"] == "running"
    assert step_states(_run(RunStatus.WAITING_EVENT, 0, [], {}))[0]["state"] == "waiting"
    failed = step_states(_run(RunStatus.FAILED, 2, [], {"docs": {"found": True}}))
    assert failed[2]["state"] == "failed"
    assert failed[0]["state"] == "done"
    # A cancelled run's current step is neither done nor failed.
    assert step_states(_run(RunStatus.CANCELLED, 2, [], {}))[2]["state"] == "pending"
    # The condition on a not-yet-reached step is only foretold while the run is
    # open: once it has completed everything before the index is simply done.
    finished = step_states(
        _run(RunStatus.COMPLETED, 5, [_event(RunEventKind.STEP_SKIPPED, "ask_docs")], {"mode": "quiet"})
    )
    assert [s["state"] for s in finished] == ["done", "skipped", "done", "done", "done"]
    # A run whose condition is still unknown shows the step as pending.
    unknown = step_states(_run(RunStatus.RUNNING, 0, [], {}))
    assert unknown[1]["state"] == "pending"


def test_step_states_tolerates_a_run_with_no_events_or_steps() -> None:
    run = SimpleNamespace(steps=None, step_index=0, status=RunStatus.RUNNING, events=None, context={})
    assert step_states(run) == []


def test_default_deadline_is_measured_from_now_in_utc() -> None:
    """Sanity for the tests that stamp ``_waits``: the engine stores ISO
    strings that ``datetime.fromisoformat`` reads back timezone-aware."""
    stamp = datetime.now(UTC).isoformat()
    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert templating._INDEX.match("verified[3]").groups() == ("verified", "3")
