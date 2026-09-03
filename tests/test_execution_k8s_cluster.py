"""k8s_cluster: K8sPodBackend against a real, reachable Kubernetes cluster (kind by default).

Tests the backend directly (ExecutionBackend.run(), not the full Gate/Broker/CognitiveLoop
vertical slice -- that's test_harness_loop_k8s_cluster.py). Requires `kubectl` on PATH and a
reachable cluster/namespace; skipped otherwise, the same shape as
test_harness_loop_linux.py's requires_root_linux."""
from __future__ import annotations

import uuid

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.execution_k8s import (
    K8sPodBackend,
    ProvisioningError,
    delete_labeled_pods,
    label_value_for,
    require_cluster_reachable,
)
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy


def _preconditions_met() -> bool:
    try:
        require_cluster_reachable()
    except ProvisioningError:
        return False
    return True


requires_k8s_cluster = pytest.mark.skipif(not _preconditions_met(), reason="needs kubectl and a reachable cluster (kind by default)")
pytestmark = [pytest.mark.k8s_cluster, requires_k8s_cluster]


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}))


@pytest.fixture
def backend() -> K8sPodBackend:
    return K8sPodBackend()


def _cleanup(execution_id: str) -> None:
    delete_labeled_pods(label_value_for(execution_id))


def test_backend_runs_real_artifact_code_in_a_real_pod(gate: Gate, backend: K8sPodBackend):
    executor = Executor(gate, backends={"k8s_pod": backend})
    intent_id = f"k8s-smoke-{uuid.uuid4().hex[:8]}"
    intent = Intent(
        kind="run_artifact", principal_id="agent-a", intent_id=intent_id, consequence="k8s",
        artifact_code="import json; print(json.dumps({'hello': 'from a real pod'}))",
    )
    try:
        decision = gate.submit(intent)
        assert decision.execution_class == "k8s_pod"
        effect = executor.execute(decision, intent)
        assert effect.execution_class == "k8s_pod"
        assert effect.detail["phase"] == "Succeeded"
        assert effect.detail["exit_code"] == 0
        assert "hello" in effect.detail["stdout"]
    finally:
        _cleanup(intent_id)
