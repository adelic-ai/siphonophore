"""linux_root_only: confirms the same_process/separate_process root-refusal for real, as real
root, on colima -- not just against a monkeypatched os.geteuid(). This is the actual claim being
made (a broker genuinely running as root cannot silently hand a "low consequence" intent full
privilege) validated against reality, matching this project's own discipline everywhere else."""
from __future__ import annotations

import os

import pytest

from siphonophore_core.execution import ExecutionError, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy

pytestmark = pytest.mark.linux_root_only

requires_real_root = pytest.mark.skipif(
    not (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs to actually be running as real root"
)


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy())


@requires_real_root
def test_same_process_genuinely_refuses_as_real_root(gate: Gate):
    assert os.geteuid() == 0  # ground truth, not assumed
    backend = SameProcessBackend()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = gate.submit(intent)
    with pytest.raises(ExecutionError, match="euid 0"):
        backend.run(decision, intent)


@requires_real_root
def test_separate_process_genuinely_refuses_as_real_root(gate: Gate):
    assert os.geteuid() == 0
    backend = SeparateProcessBackend()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="high", artifact_code="pass")
    decision = gate.submit(intent)
    with pytest.raises(ExecutionError, match="euid 0"):
        backend.run(decision, intent)


@requires_real_root
def test_same_process_allow_root_true_genuinely_runs_as_real_root(gate: Gate):
    """The escape hatch has to actually work under real root too, not just under the mock."""
    assert os.geteuid() == 0
    backend = SameProcessBackend(allow_root=True)
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="RESULT = 1")
    decision = gate.submit(intent)
    effect = backend.run(decision, intent)
    assert effect.execution_class == "same_process"
