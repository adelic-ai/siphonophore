from __future__ import annotations

import json

import pytest

from siphonophore_harness.intent_parsing import IntentParseError, parse_intent


def test_parses_a_well_formed_completion():
    completion = json.dumps({"kind": "run_artifact", "payload": {"x": 1}, "consequence": "high", "artifact_code": "pass"})
    parsed = parse_intent(completion, principal_id="alice")
    assert parsed.intent.kind == "run_artifact"
    assert parsed.intent.principal_id == "alice"
    assert parsed.intent.payload == {"x": 1}
    assert parsed.intent.consequence == "high"
    assert parsed.intent.artifact_code == "pass"
    assert parsed.intent.intent_id  # freshly generated, non-empty
    assert parsed.message is None  # no "message" field in this completion


def test_two_parses_of_the_same_completion_get_different_intent_ids():
    completion = json.dumps({"kind": "write_file"})
    a = parse_intent(completion, principal_id="alice")
    b = parse_intent(completion, principal_id="alice")
    assert a.intent.intent_id != b.intent.intent_id


def test_defaults_applied_when_optional_fields_absent():
    parsed = parse_intent(json.dumps({"kind": "write_file"}), principal_id="alice")
    assert parsed.intent.payload == {}
    assert parsed.intent.consequence == "low"
    assert parsed.intent.artifact_code is None


def test_non_json_completion_raises():
    with pytest.raises(IntentParseError):
        parse_intent("not json at all", principal_id="alice")


def test_json_array_completion_raises():
    with pytest.raises(IntentParseError):
        parse_intent(json.dumps([1, 2, 3]), principal_id="alice")


def test_missing_kind_raises():
    with pytest.raises(IntentParseError):
        parse_intent(json.dumps({"payload": {}}), principal_id="alice")


def test_unknown_field_raises():
    """A hostile or malformed completion naming a field outside the schema -- e.g. an attempt to
    smuggle a "decision" or "token" field into what becomes an Intent -- is rejected outright
    rather than silently ignored."""
    completion = json.dumps({"kind": "write_file", "token": "deadbeef", "decision": "trust me"})
    with pytest.raises(IntentParseError):
        parse_intent(completion, principal_id="alice")


def test_completion_cannot_name_its_own_intent_id():
    """Even if a completion includes an "intent_id" field, it is rejected (unknown field) --
    intent_id is never taken from untrusted text."""
    completion = json.dumps({"kind": "write_file", "intent_id": "attacker-chosen-id"})
    with pytest.raises(IntentParseError):
        parse_intent(completion, principal_id="alice")


def test_json_code_fence_with_language_tag_is_stripped():
    """Real models commonly wrap JSON in a ```json ... ``` fence even when told not to -- this is
    a formatting normalization, not a schema relaxation."""
    body = json.dumps({"kind": "write_file"})
    completion = f"```json\n{body}\n```"
    parsed = parse_intent(completion, principal_id="alice")
    assert parsed.intent.kind == "write_file"


def test_code_fence_without_language_tag_is_stripped():
    body = json.dumps({"kind": "write_file"})
    completion = f"```\n{body}\n```"
    parsed = parse_intent(completion, principal_id="alice")
    assert parsed.intent.kind == "write_file"


def test_incomplete_fence_is_left_alone_and_fails_normally():
    """Only a clean, complete fence (both sides present) is stripped -- a malformed one-sided
    fence is left untouched and fails json.loads() the same way it always did, not silently
    half-normalized into something that might parse wrong."""
    body = json.dumps({"kind": "write_file"})
    completion = f"```json\n{body}"  # no closing fence
    with pytest.raises(IntentParseError):
        parse_intent(completion, principal_id="alice")


def test_message_field_is_extracted_separately_from_the_intent():
    completion = json.dumps({"message": "hello human", "kind": "write_file"})
    parsed = parse_intent(completion, principal_id="alice")
    assert parsed.message == "hello human"
    assert parsed.intent.kind == "write_file"


def test_message_field_absent_yields_none_not_an_error():
    parsed = parse_intent(json.dumps({"kind": "write_file"}), principal_id="alice")
    assert parsed.message is None


def test_message_field_is_never_part_of_the_intent_object():
    """message is display-only -- it must never leak into the Intent that actually reaches the
    Gate, e.g. via payload or some other back door."""
    completion = json.dumps({"message": "hello human", "kind": "write_file", "payload": {"x": 1}})
    parsed = parse_intent(completion, principal_id="alice")
    assert not hasattr(parsed.intent, "message")
    assert parsed.intent.payload == {"x": 1}  # untouched by the message field
