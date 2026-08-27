from __future__ import annotations

import pytest

from siphonophore_core.execution import Executor, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker


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
