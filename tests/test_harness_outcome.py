"""Tests for classify_outcome() (siphonophore_harness/outcome.py, Stage 2 of
docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md): a pure, deterministic mapping from a
Broker.dispatch() outcome to one of the eight named categories in
docs/REFERENCE_HARNESS_TARGET_DESIGN.md section 9, grounded entirely in existing
siphonophore_core exception/return types -- never a message string or a backend-specific detail
key."""
from __future__ import annotations

import pytest

from siphonophore_core.execution import (
    ArtifactMismatchError,
    DecisionVerificationError,
    Executor,
    ExecutionError,
    NoBackendRegisteredError,
    PolicyDeniedError,
    SameProcessBackend,
)
from siphonophore_core.identity import IdentityError
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.outcome import DecisionProjection, DispatchResult, OutcomeCategory, classify_outcome


def _dispatch_result() -> DispatchResult:
    """A real, successfully-dispatched result -- not a hand-built stand-in -- for the one category
    (`executed`) that isn't an exception."""
    gate = Gate(ConsequencePolicy())
    broker = Broker(gate=gate, executor=Executor(gate, backends={"same_process": SameProcessBackend(allow_root=True)}))
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    return broker.dispatch(intent)


# ---- table-driven category mapping --------------------------------------------------------------

@pytest.mark.parametrize(
    "outcome, expected",
    [
        pytest.param(_dispatch_result(), OutcomeCategory.EXECUTED, id="executed"),
        pytest.param(IntentParseError("bad completion"), OutcomeCategory.INPUT_REJECTED, id="input_rejected"),
        pytest.param(DecisionVerificationError("forged"), OutcomeCategory.INTEGRITY_REJECTED, id="integrity_rejected-verification"),
        pytest.param(ArtifactMismatchError("swapped"), OutcomeCategory.INTEGRITY_REJECTED, id="integrity_rejected-artifact"),
        pytest.param(PolicyDeniedError("denied"), OutcomeCategory.DENIED, id="denied"),
        pytest.param(NoBackendRegisteredError("no backend"), OutcomeCategory.BACKEND_UNAVAILABLE, id="backend_unavailable"),
        pytest.param(IdentityError("checkin failed"), OutcomeCategory.IDENTITY_REJECTED, id="identity_rejected"),
        pytest.param(ExecutionError("backend failed"), OutcomeCategory.EXECUTION_FAILED, id="execution_failed"),
        pytest.param(GateViolation("authority failed Gate verification"), OutcomeCategory.AUTHORITY_REJECTED, id="authority_rejected"),
    ],
)
def test_classify_outcome_maps_every_category_by_type_alone(outcome, expected):
    assert classify_outcome(outcome) is expected


def test_authority_rejected_is_distinguished_by_absence_of_a_decision_attribute_not_by_message():
    """The one case with no distinct exception type of its own (bare GateViolation, mediation.py's
    pre-Decision refusal) is still classified structurally: by the *absence* of the `.decision`
    attribute Broker only ever attaches after a Decision was minted -- proven here with two bare
    GateViolation instances carrying deliberately different, irrelevant messages."""
    assert classify_outcome(GateViolation("authority failed Gate verification -- forged")) is OutcomeCategory.AUTHORITY_REJECTED
    assert classify_outcome(GateViolation("intent principal does not match the authority being exercised")) is OutcomeCategory.AUTHORITY_REJECTED


def test_a_bare_gate_violation_carrying_a_decision_is_not_mislabeled_as_authority_rejected():
    """The decision/intent-correspondence site (execution.py) also raises bare GateViolation, but
    is unreachable through Broker.dispatch()'s own path (docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md
    Refinement 1). If a Decision *was* minted first, classify_outcome() refuses to guess rather
    than mislabeling it as authority_rejected -- 'do not fake precision.'"""
    exc = GateViolation("decision does not correspond to this intent")
    exc.decision = DecisionProjection(permitted=True, execution_class="same_process", authority_id=None, order_id=None)
    with pytest.raises(ValueError, match="unclassifiable"):
        classify_outcome(exc)


def test_unclassifiable_type_raises_rather_than_guessing():
    class SomeOtherError(Exception):
        pass

    with pytest.raises(ValueError, match="unclassifiable"):
        classify_outcome(SomeOtherError("not a dispatch outcome at all"))


# ---- purity / substrate-neutrality --------------------------------------------------------------

def test_classification_does_not_mutate_its_argument():
    result = _dispatch_result()
    before = (result.effect, result.decision)
    classify_outcome(result)
    assert (result.effect, result.decision) == before


def test_classification_is_deterministic_and_repeatable():
    exc = PolicyDeniedError("denied")
    assert classify_outcome(exc) is classify_outcome(exc) is OutcomeCategory.DENIED


def test_classifier_never_inspects_exception_message_text():
    """Two exceptions of the same type but wildly different (even misleading) messages must
    classify identically -- proof the classifier keys on type, never on text."""
    a = PolicyDeniedError("intent 'i-1' was not permitted by policy")
    b = PolicyDeniedError("this text says 'executed successfully' but the type is what counts")
    assert classify_outcome(a) is classify_outcome(b) is OutcomeCategory.DENIED


def test_classifier_is_substrate_neutral():
    """No branch references a Kubernetes-, uid_cgroup-, or any other backend-specific concept --
    the same ExecutionError instance classifies identically regardless of which backend raised it,
    since the classifier never looks at `.detail` or backend identity at all."""
    from_uid_cgroup = ExecutionError("uid_cgroup backend requires intent.artifact_code")
    from_k8s = ExecutionError("k8s_pod 'x' did not succeed: phase='Failed' exit_code=1")
    assert classify_outcome(from_uid_cgroup) is classify_outcome(from_k8s) is OutcomeCategory.EXECUTION_FAILED
