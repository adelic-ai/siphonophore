from __future__ import annotations

import json

import pytest

from siphonophore_harness.intent_parsing import IntentParseError, parse_intent


def test_parses_a_well_formed_completion():
    completion = json.dumps({"kind": "run_artifact", "payload": {"x": 1}, "consequence": "high", "artifact_code": "pass"})
    intent = parse_intent(completion, principal_id="alice")
    assert intent.kind == "run_artifact"
    assert intent.principal_id == "alice"
    assert intent.payload == {"x": 1}
    assert intent.consequence == "high"
    assert intent.artifact_code == "pass"
    assert intent.intent_id  # freshly generated, non-empty


def test_two_parses_of_the_same_completion_get_different_intent_ids():
    completion = json.dumps({"kind": "write_file"})
    a = parse_intent(completion, principal_id="alice")
    b = parse_intent(completion, principal_id="alice")
    assert a.intent_id != b.intent_id


def test_defaults_applied_when_optional_fields_absent():
    intent = parse_intent(json.dumps({"kind": "write_file"}), principal_id="alice")
    assert intent.payload == {}
    assert intent.consequence == "low"
    assert intent.artifact_code is None


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
    intent = parse_intent(completion, principal_id="alice")
    assert intent.kind == "write_file"


def test_code_fence_without_language_tag_is_stripped():
    body = json.dumps({"kind": "write_file"})
    completion = f"```\n{body}\n```"
    intent = parse_intent(completion, principal_id="alice")
    assert intent.kind == "write_file"


def test_incomplete_fence_is_left_alone_and_fails_normally():
    """Only a clean, complete fence (both sides present) is stripped -- a malformed one-sided
    fence is left untouched and fails json.loads() the same way it always did, not silently
    half-normalized into something that might parse wrong."""
    body = json.dumps({"kind": "write_file"})
    completion = f"```json\n{body}"  # no closing fence
    with pytest.raises(IntentParseError):
        parse_intent(completion, principal_id="alice")
