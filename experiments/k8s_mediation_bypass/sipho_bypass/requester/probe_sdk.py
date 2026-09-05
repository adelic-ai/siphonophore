"""Cluster-free SDK checks -- bypass cases C2 and C3, falsification case F-08, criterion 6.

These are the only checks in the whole experiment that test a property of Siphonophore ITSELF
rather than of the deployment. They need no cluster, no credential, and no privilege: they run
entirely in R's own process against real Siphonophore types.

The distinction the pre-registration insists on, preserved here: these probes demonstrate INTERNAL
MEDIATION ENFORCEMENT (a caller that goes through `Executor` cannot get past it with a forged or
digest-mismatched Decision). They demonstrate nothing about bypass resistance, because a caller
that declines to use `Executor` never meets them -- that is bypass case B, and it is stopped by
credential custody, not by this.

Exception types are recorded from source truth (`type(exc).__name__`) as well as asserted, so that
if Siphonophore's own exception taxonomy changes, the evidence says what actually happened rather
than what this experiment expected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from siphonophore_core.execution import ArtifactMismatchError, ExecutionBackend, Executor
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy, Decision

from ..evidence import (
    MECH_ARTIFACT_DIGEST_REJECTED, MECH_BACKEND_INVOKED, MECH_GATE_REJECTED_DECISION,
    MECH_LOCAL_FAILURE, Category, CaseResult, build_case,
)

EXECUTION_CLASS = "k8s_pod"
CONSEQUENCE = "k8s"


class NeverRunsBackend(ExecutionBackend):
    """Counts invocations and produces no effect. If a probe ever reaches this, the SDK property
    under test has failed -- so the backend must be incapable of doing anything real."""

    def __init__(self) -> None:
        self.invocations = 0

    def run(self, decision: Decision, intent: Intent) -> Effect:
        self.invocations += 1
        return Effect(intent_id=intent.intent_id, execution_class=EXECUTION_CLASS, detail={"probe": True})


@dataclass
class SdkProbeResult:
    case_id: str
    raised_type: str | None = None
    is_gate_violation: bool = False
    is_artifact_mismatch: bool = False
    backend_invocations: int = 0
    observations: dict[str, Any] = field(default_factory=dict)


def _fixture(policy_kinds: tuple[str, ...] = ("run_artifact",)) -> tuple[Gate, NeverRunsBackend, Executor]:
    gate = Gate(ConsequencePolicy(mapping={CONSEQUENCE: EXECUTION_CLASS}, allowed_kinds=policy_kinds))
    backend = NeverRunsBackend()
    return gate, backend, Executor(gate, backends={EXECUTION_CLASS: backend})


def _intent(code: str, intent_id: str = "sdk-probe-1") -> Intent:
    return Intent(kind="run_artifact", principal_id="bypass-requester", intent_id=intent_id,
                  consequence=CONSEQUENCE, artifact_code=code)


def probe_forged_decision_wrong_gate() -> SdkProbeResult:
    """C2: a Decision minted by a DIFFERENT Gate. Each Gate generates its own random secret
    (mediation.py:48), so this is exactly the situation of a requester that built its own Gate --
    the strongest realistic forgery available to R, since R cannot read M's secret."""
    gate_m, backend, executor = _fixture()
    gate_r = Gate(ConsequencePolicy(mapping={CONSEQUENCE: EXECUTION_CLASS}, allowed_kinds=("run_artifact",)))
    intent = _intent("print('forged')")
    forged = gate_r.submit(intent)            # validly minted -- by the WRONG Gate
    result = SdkProbeResult(case_id="C2-forged-decision-wrong-gate")
    result.observations["forged_decision_permitted_flag"] = forged.permitted
    try:
        executor.execute(forged, intent)
    except Exception as exc:  # noqa: BLE001 -- the type IS the observation
        result.raised_type = type(exc).__name__
        result.is_gate_violation = isinstance(exc, GateViolation)
        result.is_artifact_mismatch = isinstance(exc, ArtifactMismatchError)
    result.backend_invocations = backend.invocations
    return result


def probe_fabricated_decision() -> SdkProbeResult:
    """C2 variant: a Decision constructed by hand with a made-up token. `Decision` is a plain frozen
    dataclass with no validation (policy.py:17-41), so R can always build one -- README.md
    Finding 3. What R cannot do is make `Gate.verify()` accept it."""
    gate_m, backend, executor = _fixture()
    intent = _intent("print('fabricated')")
    fabricated = Decision(
        intent_id=intent.intent_id, principal_id=intent.principal_id, kind=intent.kind,
        permitted=True, execution_class=EXECUTION_CLASS,
        artifact_digest="0" * 64, token="0" * 64,
    )
    result = SdkProbeResult(case_id="C2-fabricated-decision")
    try:
        executor.execute(fabricated, intent)
    except Exception as exc:  # noqa: BLE001
        result.raised_type = type(exc).__name__
        result.is_gate_violation = isinstance(exc, GateViolation)
        result.is_artifact_mismatch = isinstance(exc, ArtifactMismatchError)
    result.backend_invocations = backend.invocations
    return result


def probe_artifact_substitution() -> SdkProbeResult:
    """C3: a genuinely valid Decision, used with different artifact code. Expected to be refused at
    execution.py:148-154 BEFORE the backend is reached -- the ordering matters and is asserted via
    the invocation count, not inferred."""
    gate, backend, executor = _fixture()
    authorized = _intent("print('authorized')")
    decision = gate.submit(authorized)
    substituted = Intent(
        kind=authorized.kind, principal_id=authorized.principal_id, intent_id=authorized.intent_id,
        consequence=CONSEQUENCE, artifact_code="print('substituted')",
    )
    result = SdkProbeResult(case_id="C3-artifact-substitution")
    result.observations["decision_permitted"] = decision.permitted
    try:
        executor.execute(decision, substituted)
    except Exception as exc:  # noqa: BLE001
        result.raised_type = type(exc).__name__
        result.is_gate_violation = isinstance(exc, GateViolation)
        result.is_artifact_mismatch = isinstance(exc, ArtifactMismatchError)
    result.backend_invocations = backend.invocations
    return result


def _mechanism(result: SdkProbeResult, *, want_artifact_mismatch: bool) -> str:
    if result.backend_invocations > 0:
        return MECH_BACKEND_INVOKED
    if want_artifact_mismatch:
        return MECH_ARTIFACT_DIGEST_REJECTED if result.is_artifact_mismatch else MECH_LOCAL_FAILURE
    # ArtifactMismatchError subclasses GateViolation (execution.py:53), so a plain forged-token
    # rejection must be a GateViolation that is NOT an artifact mismatch, or the probe rejected for
    # a different reason than predicted and the case is inconclusive, not a pass.
    if result.is_gate_violation and not result.is_artifact_mismatch:
        return MECH_GATE_REJECTED_DECISION
    return MECH_LOCAL_FAILURE


def to_case(result: SdkProbeResult, *, want_artifact_mismatch: bool) -> CaseResult:
    expected = MECH_ARTIFACT_DIGEST_REJECTED if want_artifact_mismatch else MECH_GATE_REJECTED_DECISION
    return build_case(
        case_id=result.case_id,
        description=(
            "valid Decision used with substituted artifact_code" if want_artifact_mismatch
            else "Decision not minted by the Executor's own Gate"
        ),
        attempted_path="R-local Executor.execute() with a Decision R controls",
        expected_boundary=expected,
        observed_mechanism=_mechanism(result, want_artifact_mismatch=want_artifact_mismatch),
        # Cluster-free by construction: no substrate is reachable from this probe at all, so
        # "no mutation" is a property of the fixture rather than an unchecked assumption.
        substrate_mutation_observed=False,
        evidence_categories=(Category.S,),
        observations={
            "raised_type": result.raised_type,
            "backend_invocations": result.backend_invocations,
            **result.observations,
        },
        notes=(
            "SDK-level internal mediation enforcement only. Says nothing about bypass resistance: "
            "a caller that never invokes Executor never meets this boundary (see bypass case B)."
        ),
    )


def run_all() -> list[CaseResult]:
    return [
        to_case(probe_forged_decision_wrong_gate(), want_artifact_mismatch=False),
        to_case(probe_fabricated_decision(), want_artifact_mismatch=False),
        to_case(probe_artifact_substitution(), want_artifact_mismatch=True),
    ]
