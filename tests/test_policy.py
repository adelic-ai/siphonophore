from __future__ import annotations

from siphonophore_core.intent import Intent
from siphonophore_core.policy import ConsequencePolicy


def test_consequence_policy_maps_known_consequences():
    policy = ConsequencePolicy()
    for consequence, expected_class in [("low", "same_process"), ("high", "separate_process"), ("privileged", "uid_cgroup")]:
        intent = Intent(kind="write_file", principal_id="p", intent_id="i", consequence=consequence)
        permitted, execution_class = policy.evaluate(intent)
        assert permitted is True
        assert execution_class == expected_class


def test_consequence_policy_denies_unknown_kind():
    policy = ConsequencePolicy()
    intent = Intent(kind="delete_universe", principal_id="p", intent_id="i", consequence="low")
    permitted, _ = policy.evaluate(intent)
    assert permitted is False


def test_consequence_policy_unknown_consequence_defaults_to_same_process():
    policy = ConsequencePolicy()
    intent = Intent(kind="write_file", principal_id="p", intent_id="i", consequence="made_up")
    _, execution_class = policy.evaluate(intent)
    assert execution_class == "same_process"


def test_consequence_policy_accepts_custom_mapping_and_kinds():
    policy = ConsequencePolicy(mapping={"low": "uid_cgroup"}, allowed_kinds=("delegate",))
    permitted, execution_class = policy.evaluate(
        Intent(kind="delegate", principal_id="p", intent_id="i", consequence="low")
    )
    assert permitted is True
    assert execution_class == "uid_cgroup"

    permitted, _ = policy.evaluate(Intent(kind="write_file", principal_id="p", intent_id="i", consequence="low"))
    assert permitted is False
