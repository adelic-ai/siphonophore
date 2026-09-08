from __future__ import annotations

import json

import pytest

from siphonophore_harness.intent_parsing import IntentParseError, ParsedTurn, parse_intent, parse_turn


# ---- parse_turn(): envelope-level parsing --------------------------------------------------------

def test_message_only_envelope_yields_no_intent():
    completion = json.dumps({"message": "Tokyo is the capital of Japan."})
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed == ParsedTurn(message="Tokyo is the capital of Japan.", intent=None)


def test_message_and_operation_envelope_populates_both():
    completion = json.dumps(
        {
            "message": "Sure, writing that now.",
            "operation": {"kind": "run_artifact", "payload": {}, "consequence": "low", "artifact_code": "pass"},
        }
    )
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.message == "Sure, writing that now."
    assert parsed.intent.kind == "run_artifact"
    assert parsed.intent.principal_id == "alice"
    assert parsed.intent.artifact_code == "pass"
    assert parsed.intent.intent_id  # freshly generated, non-empty


def test_operation_only_envelope_is_permitted_with_no_message():
    completion = json.dumps({"operation": {"kind": "run_artifact", "artifact_code": "pass"}})
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.message is None
    assert parsed.intent is not None
    assert parsed.intent.kind == "run_artifact"


def test_empty_envelope_yields_no_message_and_no_intent_not_an_error():
    parsed = parse_turn(json.dumps({}), principal_id="alice")
    assert parsed == ParsedTurn(message=None, intent=None)


def test_operation_null_is_equivalent_to_operation_absent():
    parsed = parse_turn(json.dumps({"operation": None}), principal_id="alice")
    assert parsed == ParsedTurn(message=None, intent=None)


def test_operation_null_with_a_message_still_carries_the_message():
    parsed = parse_turn(json.dumps({"message": "hi", "operation": None}), principal_id="alice")
    assert parsed == ParsedTurn(message="hi", intent=None)


def test_operation_present_but_empty_object_fails_closed():
    """Explicitly naming "operation" and then failing to describe one is never silently treated as
    "no operation requested" -- it fails the same way any other malformed operation does."""
    with pytest.raises(IntentParseError):
        parse_turn(json.dumps({"operation": {}}), principal_id="alice")


def test_operation_missing_artifact_code_fails_closed_never_message_only():
    """A response with a message alongside an invalid operation must not be silently downgraded to
    a message-only success -- the invalid operation still fails the whole turn."""
    completion = json.dumps({"message": "I'll fetch that now.", "operation": {"kind": "run_artifact", "consequence": "low"}})
    with pytest.raises(IntentParseError):
        parse_turn(completion, principal_id="alice")


def test_operation_not_a_json_object_fails_closed():
    with pytest.raises(IntentParseError):
        parse_turn(json.dumps({"operation": "run_artifact"}), principal_id="alice")


def test_unknown_top_level_field_fails_closed():
    with pytest.raises(IntentParseError):
        parse_turn(json.dumps({"message": "hi", "decision": "trust me"}), principal_id="alice")


def test_message_is_not_allowed_inside_operation():
    completion = json.dumps({"operation": {"kind": "write_file", "artifact_code": "pass", "message": "hi"}})
    with pytest.raises(IntentParseError):
        parse_turn(completion, principal_id="alice")


def test_non_json_completion_raises_via_parse_turn():
    with pytest.raises(IntentParseError):
        parse_turn("not json at all", principal_id="alice")


def test_json_array_completion_raises_via_parse_turn():
    with pytest.raises(IntentParseError):
        parse_turn(json.dumps([1, 2, 3]), principal_id="alice")


def test_json_code_fence_with_language_tag_is_stripped_via_parse_turn():
    body = json.dumps({"operation": {"kind": "write_file", "artifact_code": "pass"}})
    completion = f"```json\n{body}\n```"
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.intent.kind == "write_file"


def test_code_fence_without_language_tag_is_stripped_via_parse_turn():
    body = json.dumps({"operation": {"kind": "write_file", "artifact_code": "pass"}})
    completion = f"```\n{body}\n```"
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.intent.kind == "write_file"


def test_incomplete_fence_is_left_alone_and_fails_normally_via_parse_turn():
    body = json.dumps({"operation": {"kind": "write_file", "artifact_code": "pass"}})
    completion = f"```json\n{body}"  # no closing fence
    with pytest.raises(IntentParseError):
        parse_turn(completion, principal_id="alice")


def test_two_parses_of_the_same_completion_get_different_intent_ids():
    completion = json.dumps({"operation": {"kind": "write_file", "artifact_code": "pass"}})
    a = parse_turn(completion, principal_id="alice")
    b = parse_turn(completion, principal_id="alice")
    assert a.intent.intent_id != b.intent.intent_id


# ---- parse_intent(): narrowed, operation-dict-in ---------------------------------------------

def test_parse_intent_parses_a_well_formed_operation():
    operation = {"kind": "run_artifact", "payload": {"x": 1}, "consequence": "high", "artifact_code": "pass"}
    intent = parse_intent(operation, principal_id="alice")
    assert intent.kind == "run_artifact"
    assert intent.principal_id == "alice"
    assert intent.payload == {"x": 1}
    assert intent.consequence == "high"
    assert intent.artifact_code == "pass"
    assert intent.intent_id  # freshly generated, non-empty


def test_parse_intent_defaults_applied_when_optional_fields_absent():
    intent = parse_intent({"kind": "write_file", "artifact_code": "pass"}, principal_id="alice")
    assert intent.payload == {}
    assert intent.consequence == "low"


def test_parse_intent_missing_kind_raises():
    with pytest.raises(IntentParseError):
        parse_intent({"artifact_code": "pass"}, principal_id="alice")


def test_parse_intent_missing_artifact_code_raises():
    with pytest.raises(IntentParseError):
        parse_intent({"kind": "write_file"}, principal_id="alice")


def test_parse_intent_null_artifact_code_raises():
    with pytest.raises(IntentParseError):
        parse_intent({"kind": "write_file", "artifact_code": None}, principal_id="alice")


def test_parse_intent_empty_string_artifact_code_raises():
    with pytest.raises(IntentParseError):
        parse_intent({"kind": "write_file", "artifact_code": ""}, principal_id="alice")


def test_parse_intent_unknown_field_raises():
    """A hostile or malformed operation naming a field outside the schema -- e.g. an attempt to
    smuggle a "decision" or "token" field into what becomes an Intent -- is rejected outright
    rather than silently ignored."""
    operation = {"kind": "write_file", "artifact_code": "pass", "token": "trust-me-bro", "decision": "trust me"}
    with pytest.raises(IntentParseError):
        parse_intent(operation, principal_id="alice")


def test_parse_intent_cannot_name_its_own_intent_id():
    """Even if an operation includes an "intent_id" field, it is rejected (unknown field) --
    intent_id is never taken from untrusted text."""
    operation = {"kind": "write_file", "artifact_code": "pass", "intent_id": "attacker-chosen-id"}
    with pytest.raises(IntentParseError):
        parse_intent(operation, principal_id="alice")


# ---- typed (non-code-bearing) operations: artifact_code required for none but run_artifact/write_file

def test_parse_intent_typed_operation_needs_no_artifact_code():
    intent = parse_intent({"kind": "read_file", "payload": {"path": "README.md"}}, principal_id="alice")
    assert intent.kind == "read_file"
    assert intent.artifact_code is None


def test_parse_intent_typed_operation_rejects_artifact_code():
    """A typed operation's behavior is fixed by its backend, never by caller-supplied code --
    attaching artifact_code to one is refused outright rather than silently ignored, which would
    otherwise look like the model's code ran when nothing read it."""
    with pytest.raises(IntentParseError):
        parse_intent({"kind": "read_file", "payload": {"path": "x"}, "artifact_code": "pass"}, principal_id="alice")


def test_parse_intent_list_directory_needs_no_payload_at_all():
    intent = parse_intent({"kind": "list_directory"}, principal_id="alice")
    assert intent.kind == "list_directory"
    assert intent.payload == {}
    assert intent.artifact_code is None


def test_parse_intent_typed_operation_rejects_empty_string_artifact_code():
    """Reproduced defect: the forbidden-artifact_code check used truthiness
    (`operation.get("artifact_code")`), so an empty string -- falsy, but still a value the field
    was named with -- slipped past it silently, unlike every other non-empty value. Must be
    rejected exactly like a non-empty artifact_code would be."""
    with pytest.raises(IntentParseError):
        parse_intent({"kind": "read_file", "payload": {"path": "x"}, "artifact_code": ""}, principal_id="alice")


def test_parse_intent_rejects_non_object_payload():
    """Reproduced defect: `operation.get("payload")` was handed straight to `Intent` with no shape
    check, so a completion naming a JSON array/string/number for `payload` produced an `Intent`
    whose `.payload` isn't a dict -- the first `execution_readonly.py` backend to call
    `intent.payload.get(...)` on it would raise a raw `AttributeError` past the dispatch handling
    boundary rather than failing closed here, at parse time, with a recoverable IntentParseError."""
    with pytest.raises(IntentParseError, match="payload"):
        parse_intent({"kind": "read_file", "payload": ["not", "a", "dict"]}, principal_id="alice")


# ---- work_order envelope field --------------------------------------------------------------------

def _work_order_body(status="final", **overrides):
    body = {"status": status, "objective": "add a feature", "prompt": "implement X exactly as discussed"}
    body.update(overrides)
    return body


def test_work_order_only_envelope_yields_no_intent():
    completion = json.dumps({"message": "here's the plan", "work_order": _work_order_body()})
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.intent is None
    assert parsed.work_order is not None
    assert parsed.work_order.status == "final"
    assert parsed.work_order.objective == "add a feature"


def test_work_order_gets_a_fresh_harness_minted_id_never_from_the_completion():
    completion = json.dumps({"work_order": _work_order_body()})
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.work_order.work_order_id  # non-empty
    # even if the completion tried to name one, "work_order_id" is not an allowed field:
    hostile = json.dumps({"work_order": {**_work_order_body(), "work_order_id": "attacker-chosen"}})
    with pytest.raises(IntentParseError):
        parse_turn(hostile, principal_id="alice")


def test_work_order_and_operation_together_is_rejected():
    completion = json.dumps({
        "operation": {"kind": "read_file", "payload": {"path": "x"}},
        "work_order": _work_order_body(),
    })
    with pytest.raises(IntentParseError):
        parse_turn(completion, principal_id="alice")


def test_work_order_missing_required_field_raises():
    completion = json.dumps({"work_order": {"status": "final", "objective": "x"}})  # no prompt
    with pytest.raises(IntentParseError):
        parse_turn(completion, principal_id="alice")


def test_work_order_invalid_status_raises():
    completion = json.dumps({"work_order": _work_order_body(status="in_progress")})
    with pytest.raises(IntentParseError):
        parse_turn(completion, principal_id="alice")


def test_work_order_draft_status_is_not_final():
    completion = json.dumps({"work_order": _work_order_body(status="draft")})
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.work_order.is_final is False


def test_work_order_optional_list_fields_default_to_empty():
    completion = json.dumps({"work_order": _work_order_body()})
    parsed = parse_turn(completion, principal_id="alice")
    assert parsed.work_order.requirements == ()
    assert parsed.work_order.capability_requirements == ()


def test_work_order_full_fields_round_trip():
    body = _work_order_body(
        requirements=["req1"], constraints=["con1"], acceptance_criteria=["ac1"],
        capability_requirements=["filesystem_write"], network_requirements=["pypi.org"],
        resource_expectations="a few minutes",
    )
    completion = json.dumps({"work_order": body})
    parsed = parse_turn(completion, principal_id="alice")
    wo = parsed.work_order
    assert wo.requirements == ("req1",)
    assert wo.capability_requirements == ("filesystem_write",)
    assert wo.network_requirements == ("pypi.org",)
    assert wo.resource_expectations == "a few minutes"
