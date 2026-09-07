"""Tests for siphonophore_harness/composition.py (Stage 3 of
docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md): the named "portable" execution profile and the
generic, substrate-neutral compose_profile() convenience underneath it.

Direct Gate/Executor/Broker construction (test_harness_broker.py, test_harness_loop_k8s_cluster.py,
test_harness_loop_linux.py) is untouched by this file and remains equally valid -- this module adds
a convenience, not a replacement."""
from __future__ import annotations

import dataclasses

import pytest

from siphonophore_core.execution import ExecutionBackend, Executor, PolicyDeniedError
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.composition import (
    ExecutionProfile,
    compose_kind_profile,
    compose_profile,
    planning_profile,
    portable_profile,
)


class _CountingBackend(ExecutionBackend):
    """Test-only fake backend, mirroring the counting-backend convention already used by
    tests/test_harness_loop_k8s_cluster.py -- proves compose_profile() actually wires the backend
    it was given, not merely that it accepts one."""

    def __init__(self, execution_class: str) -> None:
        self.call_count = 0
        self._execution_class = execution_class

    def run(self, decision, intent) -> Effect:
        self.call_count += 1
        return Effect(intent_id=intent.intent_id, execution_class=self._execution_class, detail={})


# ---- portable profile: construction --------------------------------------------------------

def test_portable_profile_constructs_without_privileged_dependencies():
    """No root, no cluster, no environment beyond a plain Python process -- if this needed either,
    it would fail on this portable test run."""
    profile = portable_profile()
    assert profile.name == "portable"


def test_portable_profile_creates_a_valid_gate():
    profile = portable_profile()
    assert isinstance(profile.gate, Gate)


def test_portable_profile_creates_an_executor_wired_with_both_portable_backends():
    profile = portable_profile()
    assert isinstance(profile.executor, Executor)
    assert profile.execution_classes == ("same_process", "separate_process")


def test_portable_profile_creates_a_broker_using_that_gate_and_executor():
    """Black-box identity proof, not a private-attribute reach-in: register a replacement backend
    directly on profile.executor after construction, then dispatch through profile.broker -- this
    only takes effect if the Broker was actually built with this exact Executor instance, not a
    separately constructed one."""
    profile = portable_profile()
    assert isinstance(profile.broker, Broker)

    counting = _CountingBackend("separate_process")
    profile.executor.register_backend("separate_process", counting)

    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-identity", consequence="high", artifact_code="pass")
    profile.broker.dispatch(intent)
    assert counting.call_count == 1


# ---- portable profile: the Broker it builds actually mediates ------------------------------

def test_portable_profile_broker_executes_allowed_requests_via_both_backends():
    profile = portable_profile()
    low = Intent(kind="run_artifact", principal_id="alice", intent_id="i-low", consequence="low", artifact_code="pass")
    result_low = profile.broker.dispatch(low)
    assert result_low.execution_class == "same_process"

    high = Intent(kind="run_artifact", principal_id="alice", intent_id="i-high", consequence="high", artifact_code="pass")
    result_high = profile.broker.dispatch(high)
    assert result_high.execution_class == "separate_process"


def test_portable_profile_broker_denies_policy_denied_requests():
    profile = portable_profile()
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-deny", consequence="low")
    with pytest.raises(PolicyDeniedError):
        profile.broker.dispatch(intent)


# ---- discoverability: execution classes -----------------------------------------------------

def test_portable_profile_exposes_registered_execution_classes():
    profile = portable_profile()
    assert profile.execution_classes == ("same_process", "separate_process")


def test_execution_classes_match_the_actual_configured_backends():
    counting = _CountingBackend("custom_tier")
    profile = compose_profile("check", backends={"custom_tier": counting}, policy_mapping={})
    assert profile.execution_classes == ("custom_tier",)


def test_execution_classes_ordering_is_deterministic():
    backends = {"zeta_tier": _CountingBackend("zeta_tier"), "alpha_tier": _CountingBackend("alpha_tier")}
    profile = compose_profile("order-check", backends=backends, policy_mapping={})
    assert profile.execution_classes == ("alpha_tier", "zeta_tier")


def test_execution_classes_are_plain_strings_not_backend_objects():
    profile = portable_profile()
    for execution_class in profile.execution_classes:
        assert isinstance(execution_class, str)


# ---- discoverability: policy mapping ---------------------------------------------------------

def test_portable_profile_exposes_active_policy_mapping():
    profile = portable_profile()
    assert dict(profile.policy_mapping) == ConsequencePolicy.DEFAULT_MAPPING


def test_policy_mapping_projection_is_read_only():
    profile = portable_profile()
    with pytest.raises(TypeError):
        profile.policy_mapping["low"] = "separate_process"  # type: ignore[index]


def test_mutating_a_copy_of_the_policy_mapping_cannot_affect_the_underlying_policy():
    profile = portable_profile()
    mapping_copy = dict(profile.policy_mapping)
    mapping_copy["low"] = "separate_process"

    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-verify", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    assert result.execution_class == "same_process"


def test_compose_profile_copies_caller_supplied_dicts_rather_than_referencing_them():
    original_backends = {"custom_tier": _CountingBackend("custom_tier")}
    original_mapping = {"weird": "custom_tier"}
    profile = compose_profile("copy-check", backends=original_backends, policy_mapping=original_mapping)

    original_backends["extra_tier"] = _CountingBackend("extra_tier")
    original_mapping["another"] = "extra_tier"

    assert profile.execution_classes == ("custom_tier",)
    assert dict(profile.policy_mapping) == {"weird": "custom_tier"}


# ---- discoverability: no sensitive/substrate-specific leakage --------------------------------

def test_execution_profile_exposes_no_gate_secret_or_decision_token_fields():
    field_names = {f.name for f in dataclasses.fields(ExecutionProfile)}
    assert "token" not in field_names
    assert "secret" not in field_names


def test_execution_profile_has_no_substrate_specific_field_names():
    field_names = {f.name for f in dataclasses.fields(ExecutionProfile)}
    banned = {"pod", "namespace", "kubectl", "service_account", "container"}
    assert not (field_names & banned)


# ---- custom composition: the mechanism is generic, not portable-only -------------------------

def test_compose_profile_accepts_a_custom_backend_and_policy_mapping():
    """Proves compose_profile() is generic composition, not a hardcoded portable-only framework:
    a custom execution class ("custom_tier") and a custom consequence mapping (unrelated to any
    real substrate) work through the exact same function portable_profile() itself calls, with no
    branch anywhere in composition.py naming "portable" or any real substrate string."""
    counting = _CountingBackend("custom_tier")
    profile = compose_profile(
        "custom",
        backends={"custom_tier": counting},
        policy_mapping={"weird": "custom_tier"},
    )
    assert profile.name == "custom"
    assert profile.execution_classes == ("custom_tier",)
    assert dict(profile.policy_mapping) == {"weird": "custom_tier"}

    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-custom", consequence="weird", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    assert result.execution_class == "custom_tier"
    assert counting.call_count == 1


# ---- planning profile: typed observational operations only, no code-execution backend ----------

def test_planning_profile_registers_only_observational_backends():
    profile = planning_profile(root=".")
    assert set(profile.execution_classes) == {"read_file", "list_directory", "search_repository"}
    assert "same_process" not in profile.execution_classes
    assert "separate_process" not in profile.execution_classes
    assert set(profile.allowed_kinds) == {"read_file", "list_directory", "search_repository"}
    assert "run_artifact" not in profile.allowed_kinds
    assert "write_file" not in profile.allowed_kinds


def test_portable_profile_allowed_kinds_matches_default_consequence_policy_kinds():
    profile = portable_profile()
    assert set(profile.allowed_kinds) == set(ConsequencePolicy.DEFAULT_ALLOWED_KINDS)


def test_planning_profile_denies_run_artifact_and_write_file(tmp_path):
    profile = planning_profile(root=str(tmp_path))
    for kind in ("run_artifact", "write_file"):
        intent = Intent(kind=kind, principal_id="alice", intent_id=f"i-{kind}", consequence="low")
        decision = profile.gate.submit(intent)
        assert decision.permitted is False


def test_planning_profile_reads_a_real_file_end_to_end(tmp_path):
    (tmp_path / "README.md").write_text("hello from the sandbox")
    profile = planning_profile(root=str(tmp_path))
    intent = Intent(
        kind="read_file", principal_id="alice", intent_id="i-read", consequence="low",
        payload={"path": "README.md"},
    )
    result = profile.broker.dispatch(intent)
    assert result.execution_class == "read_file"
    assert result.detail["content"] == "hello from the sandbox"


def test_planning_profile_confines_reads_to_its_configured_root(tmp_path):
    profile = planning_profile(root=str(tmp_path))
    intent = Intent(
        kind="read_file", principal_id="alice", intent_id="i-escape", consequence="low",
        payload={"path": "/etc/passwd"},
    )
    with pytest.raises(Exception):  # PathEscapesRootError, a GateViolation-unrelated ExecutionError
        profile.broker.dispatch(intent)


def test_compose_kind_profile_defaults_to_identity_mapping():
    counting = _CountingBackend("widget")
    profile = compose_kind_profile("widgets", backends={"widget": counting})
    assert dict(profile.policy_mapping) == {"widget": "widget"}
    intent = Intent(kind="widget", principal_id="alice", intent_id="i-w", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    assert result.execution_class == "widget"


def test_compose_kind_profile_denies_kinds_outside_its_mapping():
    counting = _CountingBackend("widget")
    profile = compose_kind_profile("widgets", backends={"widget": counting})
    intent = Intent(kind="gadget", principal_id="alice", intent_id="i-g", consequence="low", artifact_code="pass")
    decision = profile.gate.submit(intent)
    assert decision.permitted is False
    assert counting.call_count == 0
