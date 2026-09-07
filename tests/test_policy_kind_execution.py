"""Tests for KindExecutionPolicy (policy.py): routes execution_class directly by intent.kind,
independent of ConsequencePolicy's tier-based shape."""
from __future__ import annotations

from siphonophore_core.intent import Intent
from siphonophore_core.policy import KindExecutionPolicy


def _intent(kind: str, consequence: str = "low") -> Intent:
    return Intent(kind=kind, principal_id="alice", intent_id="i-1", consequence=consequence)


def test_known_kind_is_permitted_and_routed_to_its_own_execution_class():
    policy = KindExecutionPolicy({"read_file": "read_file", "list_directory": "list_directory"})
    permitted, execution_class = policy.evaluate(_intent("read_file"))
    assert permitted is True
    assert execution_class == "read_file"


def test_unknown_kind_is_denied():
    policy = KindExecutionPolicy({"read_file": "read_file"})
    permitted, execution_class = policy.evaluate(_intent("run_artifact"))
    assert permitted is False
    assert execution_class == ""


def test_consequence_field_is_ignored_entirely():
    policy = KindExecutionPolicy({"read_file": "read_file"})
    low = policy.evaluate(_intent("read_file", consequence="low"))
    privileged = policy.evaluate(_intent("read_file", consequence="privileged"))
    assert low == privileged == (True, "read_file")
