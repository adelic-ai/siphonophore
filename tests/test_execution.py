"""Tests for Executor and the portable backends (same_process, separate_process). Portable --
no root needed. uid_cgroup is tested separately (test_execution_uid_cgroup.py), same reason
lab/001-003 stayed simple before lab/004 introduced privilege separation on purpose."""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from siphonophore_core.execution import (
    ArtifactMismatchError,
    DecisionVerificationError,
    ExecutionError,
    Executor,
    NoBackendRegisteredError,
    PolicyDeniedError,
    SameProcessBackend,
    SeparateProcessBackend,
)
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy, Decision


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy())


@pytest.fixture
def executor(gate: Gate) -> Executor:
    # allow_root=True: this file tests dispatch logic (Gate/Executor/backend behavior), not the
    # root-refusal feature itself (see test_execution_root_refusal.py) -- the full suite is also
    # run for real as root on colima (test_execution_uid_cgroup.py etc.), and these tests should
    # exercise the same dispatch logic there too, not incidentally hit the new refusal.
    backends = {
        "same_process": SameProcessBackend(allow_root=True),
        "separate_process": SeparateProcessBackend(allow_root=True),
    }
    return Executor(gate, backends=backends)


def test_same_process_runs_authorized_artifact(executor: Executor):
    intent = Intent(
        kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low",
        payload={"marker": "hello"}, artifact_code="RESULT = payload['marker'] * 2",
    )
    decision = executor._gate.submit(intent)
    assert decision.execution_class == "same_process"
    effect = executor.execute(decision, intent)
    assert effect.execution_class == "same_process"


def test_separate_process_runs_authorized_artifact_and_reports_its_own_pid(executor: Executor):
    code = "import json,os,sys; print(json.dumps({'pid': os.getpid()}))"
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="high", artifact_code=code)
    decision = executor._gate.submit(intent)
    assert decision.execution_class == "separate_process"
    effect = executor.execute(decision, intent)
    import json

    reported = json.loads(effect.detail["stdout"])
    assert reported["pid"] != os.getpid()  # a REAL, different process, not the test's own


def test_swapped_artifact_is_refused_before_running(executor: Executor):
    """The genuinely-minted Decision authorizes program A; the Intent handed to execute() carries
    program B instead. Gate.verify() alone would return True (the Decision itself is untouched) --
    catching this requires Executor's own independent re-hash."""
    program_a = "import sys; sys.exit(0)"
    program_b = "import sys; sys.exit(1)"
    real_intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="high", artifact_code=program_a)
    decision = executor._gate.submit(real_intent)
    swapped_intent = replace(real_intent, artifact_code=program_b)

    assert executor._gate.verify(decision) is True  # the Decision itself was never touched
    with pytest.raises(ArtifactMismatchError):
        executor.execute(decision, swapped_intent)


def test_forged_decision_refused(executor: Executor):
    forged = Decision(
        intent_id="i-1", principal_id="alice", kind="run_artifact",
        permitted=True, execution_class="same_process", artifact_digest="",
        token="0" * 64,
    )
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    with pytest.raises(GateViolation):
        executor.execute(forged, intent)


def test_denied_decision_refused(executor: Executor):
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-1", consequence="low")
    decision = executor._gate.submit(intent)
    assert decision.permitted is False
    with pytest.raises(GateViolation):
        executor.execute(decision, intent)


def test_no_backend_registered_for_unknown_execution_class(gate: Gate):
    executor = Executor(gate, backends={})  # deliberately empty registry
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = gate.submit(intent)
    with pytest.raises(GateViolation):
        executor.execute(decision, intent)


# ---- Stage 1 (docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md): narrow GateViolation subclasses ---
# The three tests above are left unmodified as compatibility evidence -- broad `GateViolation`
# still catches every one of these conditions. The tests below prove the *specific* subclass is
# what's actually raised at each site now, without touching the broad assertions.

def test_forged_decision_raises_decision_verification_error(executor: Executor):
    forged = Decision(
        intent_id="i-1", principal_id="alice", kind="run_artifact",
        permitted=True, execution_class="same_process", artifact_digest="",
        token="0" * 64,
    )
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    with pytest.raises(DecisionVerificationError) as exc_info:
        executor.execute(forged, intent)
    assert isinstance(exc_info.value, GateViolation)


def test_denied_decision_raises_policy_denied_error(executor: Executor):
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-1", consequence="low")
    decision = executor._gate.submit(intent)
    assert decision.permitted is False
    with pytest.raises(PolicyDeniedError) as exc_info:
        executor.execute(decision, intent)
    assert isinstance(exc_info.value, GateViolation)


def test_no_backend_registered_raises_no_backend_registered_error(gate: Gate):
    executor = Executor(gate, backends={})  # deliberately empty registry
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = gate.submit(intent)
    with pytest.raises(NoBackendRegisteredError) as exc_info:
        executor.execute(decision, intent)
    assert isinstance(exc_info.value, GateViolation)


def test_new_subclasses_are_siblings_not_related_to_each_other_or_to_artifact_mismatch():
    """Structural guard: the three new subclasses and the pre-existing ArtifactMismatchError are
    all direct GateViolation subclasses, not related to one another -- catching one must not
    accidentally catch another."""
    new_types = (DecisionVerificationError, PolicyDeniedError, NoBackendRegisteredError)
    for cls in new_types:
        assert issubclass(cls, GateViolation)
        assert not issubclass(cls, ArtifactMismatchError)
        assert not issubclass(ArtifactMismatchError, cls)
    assert len(set(new_types) | {ArtifactMismatchError}) == 4


def test_decision_intent_mismatch_is_unaffected_by_this_stage(executor: Executor):
    """Out of scope for Stage 1 (docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md Refinement 1):
    decision/intent correspondence failure keeps raising the bare GateViolation base class,
    unchanged -- this stage only adds specificity to the three sites named in the plan."""
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = executor._gate.submit(intent)
    mismatched_intent = replace(intent, intent_id="i-different")
    with pytest.raises(GateViolation) as exc_info:
        executor.execute(decision, mismatched_intent)
    assert type(exc_info.value) is GateViolation


def test_register_backend_extends_dispatch_without_touching_executor():
    """DESIGN.md section 6's extension point: a new execution class is a new ExecutionBackend
    registered on an existing Executor, not a modification to Executor's own dispatch logic."""
    from siphonophore_core.execution import ExecutionBackend
    from siphonophore_core.intent import Effect

    class FakeContainerBackend(ExecutionBackend):
        def run(self, decision, intent):
            return Effect(intent_id=intent.intent_id, execution_class="container", detail={"fake": True})

    gate = Gate(ConsequencePolicy(mapping={"low": "container"}))
    executor = Executor(gate)
    executor.register_backend("container", FakeContainerBackend())

    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = gate.submit(intent)
    assert decision.execution_class == "container"
    effect = executor.execute(decision, intent)
    assert effect.detail == {"fake": True}


def test_same_process_backend_requires_artifact_code(executor: Executor):
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low")  # no artifact_code
    decision = executor._gate.submit(intent)
    with pytest.raises(ExecutionError):
        executor.execute(decision, intent)
