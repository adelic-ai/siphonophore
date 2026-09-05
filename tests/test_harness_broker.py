from __future__ import annotations

import dataclasses

import pytest

from siphonophore_core.authority import Authority, Scope
from siphonophore_core.execution import Executor, NoBackendRegisteredError, PolicyDeniedError, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy, Decision
from siphonophore_harness.broker import Broker
from siphonophore_harness.outcome import DecisionProjection, DispatchResult


def _executor(gate: Gate) -> Executor:
    # allow_root=True: this file tests Broker's own dispatch logic, not the root-refusal feature
    # (see test_execution_root_refusal.py) -- the full suite also runs as real root on colima, and
    # these portable tests should exercise the same logic there too, not incidentally hit the
    # refusal same_process/separate_process now raise by default when euid=0.
    backends = {
        "same_process": SameProcessBackend(allow_root=True),
        "separate_process": SeparateProcessBackend(allow_root=True),
    }
    return Executor(gate, backends=backends)


def test_dispatch_runs_an_authorized_intent():
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    effect = b.dispatch(intent)
    assert effect.execution_class == "same_process"


def test_dispatch_refuses_a_denied_intent():
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-1", consequence="low")
    with pytest.raises(GateViolation):
        b.dispatch(intent)


def test_delegate_is_not_an_ordinary_kind_broker_will_dispatch():
    """Historical note, not a regression: an earlier version of this test asserted that a
    "delegate"-kind Intent dispatched through Broker identically to "run_artifact" -- offered as
    DESIGN.md section 7's delegation-reduces-to-the-same-primitive proof. It wasn't: dispatching an
    inert kind string through the same call proves repeated mediation, not delegated authority
    provenance (see HISTORY.md). "delegate" is no longer an Intent kind at all -- real delegation is
    now Gate.delegate()/authority.py's Order->Authority mechanism, a grant, not an attempted effect.
    This test documents that "delegate" is correctly refused as an ordinary kind now, not silently
    treated as one -- see test_authority.py and test_harness_loop_linux.py's
    test_a_delegates_constrained_authority_to_b_who_executes_via_uid_cgroup for the real mechanism."""
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="delegate", principal_id="alice", intent_id="i-delegate", consequence="low", artifact_code="pass")
    with pytest.raises(GateViolation):
        b.dispatch(intent)


def test_dispatch_with_authority_exercises_delegated_scope():
    """Portable counterpart to test_harness_loop_linux.py's real end-to-end slice -- proves
    Broker.dispatch(intent, authority=...) works with no root/Linux needed, since the authority
    mechanism itself is pure Gate logic. B holds a real Authority delegated from A; B's own effect
    goes through the public Broker interface only."""
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    intent = Intent(kind="run_artifact", principal_id="agent-a.sub-agent-b", intent_id="i-deleg-1",
                     consequence="low", artifact_code="pass")
    effect = b.dispatch(intent, authority=authority_b)
    assert effect.execution_class == "same_process"


def test_dispatch_with_authority_refuses_scope_expansion():
    """B attempts a kind outside what A delegated -- refused through the same public interface."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    b = Broker(gate=gate, executor=_executor(gate))
    order = gate.issue_order("order-2", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    out_of_scope = Intent(kind="write_file", principal_id="agent-a.sub-agent-b", intent_id="i-deleg-2", consequence="low")
    with pytest.raises(GateViolation):
        b.dispatch(out_of_scope, authority=authority_b)


# ---- Stage 2 (docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md): structured dispatch outcome -------
# dispatch() now returns a DispatchResult on success and attaches a DecisionProjection to
# post-Decision refusals. The tests above are left unmodified as compatibility evidence -- they
# still pass because DispatchResult delegates .execution_class/.intent_id/.detail to the wrapped
# Effect, exactly like a bare Effect would.

def test_dispatch_returns_a_dispatch_result_wrapping_effect_and_a_decision_projection():
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")

    result = b.dispatch(intent)

    assert isinstance(result, DispatchResult)
    assert isinstance(result.effect, Effect)
    assert isinstance(result.decision, DecisionProjection)
    assert result.decision.permitted is True
    assert result.decision.execution_class == "same_process"
    assert result.decision.authority_id is None  # authority-less path, unchanged from before
    assert result.decision.order_id is None


def test_dispatch_result_delegates_effect_attributes_for_compatibility():
    """The compatibility surface this stage depends on: existing code reading .intent_id/
    .execution_class/.detail off what dispatch() returns (CognitiveLoop, examples/repl.py) needs
    no change, because DispatchResult reads these straight through to the wrapped Effect."""
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-42", consequence="low", artifact_code="pass")

    result = b.dispatch(intent)

    assert result.intent_id == result.effect.intent_id == "i-42"
    assert result.execution_class == result.effect.execution_class == "same_process"
    assert result.detail == result.effect.detail


def test_dispatch_result_projection_excludes_token_and_artifact_digest():
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")

    result = b.dispatch(intent)

    projection_fields = {f.name for f in dataclasses.fields(result.decision)}
    assert projection_fields == {"permitted", "execution_class", "authority_id", "order_id"}
    assert "token" not in projection_fields
    assert "artifact_digest" not in projection_fields
    # Not merely absent from the field set -- there is no raw Decision object reachable from the
    # result at all for a holder to read `.token`/`.artifact_digest` off of.
    assert not isinstance(result.decision, Decision)
    assert not hasattr(result, "raw_decision")
    for value in dataclasses.asdict(result.decision).values():
        assert value != "0" * 64  # not a plausible spot-check for a leaked hex token


def test_denied_dispatch_attaches_decision_projection_to_the_exception():
    """PolicyDeniedError is reachable through Broker's own path (an ordinary DENY) -- the
    identical exception TYPE Stage 1 introduced is still what's raised; this stage only adds a
    `.decision` attribute carrying the curated projection."""
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-1", consequence="low")

    with pytest.raises(PolicyDeniedError) as exc_info:
        b.dispatch(intent)

    assert isinstance(exc_info.value, GateViolation)  # broad compatibility preserved
    projection = exc_info.value.decision
    assert isinstance(projection, DecisionProjection)
    assert projection.permitted is False
    assert not hasattr(projection, "token")
    assert not hasattr(projection, "artifact_digest")


def test_no_backend_registered_dispatch_attaches_decision_projection_to_the_exception():
    """NoBackendRegisteredError is reachable through Broker's own path (policy resolves to an
    execution_class nothing is registered for)."""
    gate = Gate(ConsequencePolicy(mapping={"low": "uid_cgroup"}))  # not registered below
    b = Broker(gate=gate, executor=Executor(gate, backends={"same_process": SameProcessBackend(allow_root=True)}))
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")

    with pytest.raises(NoBackendRegisteredError) as exc_info:
        b.dispatch(intent)

    assert isinstance(exc_info.value, GateViolation)
    projection = exc_info.value.decision
    assert projection.permitted is True  # policy allowed it; the gap is purely a missing backend
    assert projection.execution_class == "uid_cgroup"


def test_authority_rejection_before_any_decision_does_not_fabricate_a_projection():
    """Pre-Decision failures (Gate.submit() itself raising, before Executor.execute() is ever
    called) must not receive a fabricated Decision context -- there is no Decision to project."""
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))
    forged_authority = Authority(
        authority_id="forged", principal_id="alice", order_id="order-x", parent_authority_id=None,
        scope=Scope(allowed_kinds=frozenset({"run_artifact"}), remaining_delegation_depth=0),
        token="0" * 64,  # never minted by this Gate -- verify_authority() will reject it
    )
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")

    with pytest.raises(GateViolation) as exc_info:
        b.dispatch(intent, authority=forged_authority)

    assert getattr(exc_info.value, "decision", None) is None
