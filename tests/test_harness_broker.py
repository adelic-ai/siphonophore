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


def test_delegate_kind_dispatches_through_the_identical_call_as_run_artifact():
    """DESIGN.md section 7: delegation must reduce to the exact same primitive a tool call does,
    not a separately-mediated mechanism. Same Broker instance, same dispatch() call, two different
    intent kinds landing on the same execution class with zero special-casing for "delegate"."""
    gate = Gate(ConsequencePolicy())
    b = Broker(gate=gate, executor=_executor(gate))

    tool_call = Intent(kind="run_artifact", principal_id="alice", intent_id="i-tool", consequence="low", artifact_code="pass")
    delegation = Intent(kind="delegate", principal_id="alice", intent_id="i-delegate", consequence="low", artifact_code="pass")

    tool_effect = b.dispatch(tool_call)
    delegate_effect = b.dispatch(delegation)

    assert tool_effect.execution_class == delegate_effect.execution_class == "same_process"
