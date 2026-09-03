"""k8s_cluster: the real Gate -> Broker -> Executor -> K8sPodBackend vertical slice on a real
Kubernetes cluster (kind by default), proving:

1. An ALLOW dispatch actually runs a real Pod, and what happened is checked two separate ways:
   internally (K8sPodBackend.run() was actually invoked once, and the Effect it returned), and
   externally (a fresh, independent `kubectl get`/`kubectl logs` call this test makes on its own,
   never reading through Siphonophore's own claimed Effect for the *content* of what it asserts --
   only for the intent_id used to correlate which Pod to look at, which is exactly the shape
   docket approved: "AgentWatch ... may use an intent_id-derived workload label/name for
   after-the-fact correlation. The independence requirement is that its observation comes from its
   own Kubernetes ... vantage rather than accepting Siphonophore's execution claim as evidence.").

2. A DENY dispatch never reaches Kubernetes at all -- checked the same two ways: internally
   (K8sPodBackend.run() was never invoked, not merely "no exception logged run() succeeding") and
   externally, where practical, that a fresh cluster query finds no corresponding Pod. The two
   DENY tests below carry genuinely different evidentiary weight, disclosed rather than papered
   over: `test_deny_via_direct_dispatch_never_touches_kubernetes` can query by the exact
   intent-id label, since the caller supplies `intent_id` directly; its external check is mostly
   *implied* by the internal call-counter (if run() is never called, nothing could have created
   that label) but still guards against the counter itself being wrong, or an unanticipated second
   path to Kubernetes bypassing the registered backend. `test_deny_via_cognitive_loop_never_touches_kubernetes`
   has no intent_id to key on -- `intent_parsing.parse_intent()` mints one internally
   (`uuid.uuid4()`) and it never reaches the caller when GateViolation propagates before an Effect
   exists -- so its external check is a strictly weaker before/after total-managed-Pod-count
   invariant, not a label-specific absence proof. Real evidence, not vacuous, but named as weaker
   here rather than left to look equivalent to the direct-dispatch case.

3. `test_direct_dispatch_and_cognitive_loop_reach_the_identical_backend_instance` runs ONE shared
   Gate/Executor/Broker/backend instance through both a direct `Broker.dispatch()` call and a
   `CognitiveLoop.step()` call (a model-produced completion, not a directly-constructed Intent),
   asserting the shared call-counter reaches 2 -- literally the same object instance handling both
   call shapes, not merely "the same backend class, registered the same way, behaves consistently
   under each caller" (a materially weaker claim an earlier version of this file only supported by
   accident, each test having called `_wiring()` independently). Mirrors
   test_harness_loop_linux.py's own `test_two_real_cognitive_loops_agent_a_delegates_to_agent_b`,
   which shares one Gate/Executor/Broker across two live CognitiveLoop instances for the identical
   reason.

No check-in/identity-binding tier exists for `k8s_pod` (see execution_k8s.py's own docstring and
docs/EXECUTION_K8S.md) -- this file does not attempt to prove anything stronger than "the Pod that
ran is the Pod Siphonophore's own claim points at, and Kubernetes independently agrees it ran and
succeeded"."""
from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from siphonophore_core.execution import Executor, ExecutionBackend
from siphonophore_core.execution_k8s import (
    K8sPodBackend,
    ProvisioningError,
    delete_labeled_pods,
    label_value_for,
    require_cluster_reachable,
)
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.model import ScriptedModel


def _preconditions_met() -> bool:
    try:
        require_cluster_reachable()
    except ProvisioningError:
        return False
    return True


requires_k8s_cluster = pytest.mark.skipif(not _preconditions_met(), reason="needs kubectl and a reachable cluster (kind by default)")
pytestmark = [pytest.mark.k8s_cluster, requires_k8s_cluster]

NAMESPACE = "default"


class _CountingK8sPodBackend(ExecutionBackend):
    """Test-only instrumentation, not production code: wraps a real K8sPodBackend and counts
    invocations of run(), so a DENY case can assert Kubernetes was never touched -- directly,
    rather than inferring non-invocation from the mere absence of an exception or an Effect."""

    def __init__(self, real: K8sPodBackend) -> None:
        self._real = real
        self.call_count = 0

    def run(self, decision, intent) -> Effect:
        self.call_count += 1
        return self._real.run(decision, intent)


def _independent_pods_for_intent(intent_id: str) -> list[dict]:
    """A fresh kubectl call this test makes on its own -- not a read of anything Siphonophore's
    own Effect claims. Uses intent_id only as the correlation key (the label this backend attaches
    to the Pod it creates), exactly the after-the-fact-correlation shape the design review
    approved -- what's asserted about Pod state below comes from this call, not from `effect`."""
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", f"siphonophore.dev/intent-id={label_value_for(intent_id)}", "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)["items"]


def _independent_managed_pod_count() -> int:
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "get", "pods", "-l", "app.kubernetes.io/managed-by=siphonophore", "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    return len(json.loads(result.stdout)["items"])


def _independent_logs(pod_name: str) -> str:
    result = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "logs", f"pod/{pod_name}", "--container=artifact"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _wiring():
    gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}))
    counting = _CountingK8sPodBackend(K8sPodBackend())
    executor = Executor(gate, backends={"k8s_pod": counting})
    broker = Broker(gate=gate, executor=executor)
    return gate, counting, broker


# ---- the identical-instance proof: one shared wiring, both caller shapes ----------------------

def test_direct_dispatch_and_cognitive_loop_reach_the_identical_backend_instance():
    """One Gate/Executor/Broker/counting-backend instance, exercised first by a direct
    Broker.dispatch() call (external-harness-shaped) and then by a CognitiveLoop.step() call
    (reference-harness-shaped) -- proving the *identical* object instance handled both call
    shapes, not merely that two separately-wired instances of the same class both happened to
    work."""
    _gate, counting, broker = _wiring()

    # -- direct dispatch --
    direct_intent_id = f"k8s-allow-direct-{uuid.uuid4().hex[:8]}"
    direct_marker = f"marker-{uuid.uuid4().hex[:8]}"
    intent = Intent(
        kind="run_artifact", principal_id="agent-a", intent_id=direct_intent_id, consequence="k8s",
        artifact_code=f"import json; print(json.dumps({{'marker': {direct_marker!r}}}))",
    )
    loop_intent_id = None
    try:
        effect = broker.dispatch(intent)

        # Internal.
        assert effect.execution_class == "k8s_pod"
        assert counting.call_count == 1
        assert effect.detail["phase"] == "Succeeded"
        assert effect.detail["exit_code"] == 0

        # External: independently re-derived, not read off `effect`.
        pods = _independent_pods_for_intent(direct_intent_id)
        assert len(pods) == 1
        assert pods[0]["status"]["phase"] == "Succeeded"
        assert pods[0]["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] == 0
        assert direct_marker in _independent_logs(pods[0]["metadata"]["name"])

        # -- CognitiveLoop, same broker/backend instance --
        loop_marker = f"marker-{uuid.uuid4().hex[:8]}"
        completion = json.dumps({
            "kind": "run_artifact", "consequence": "k8s",
            "artifact_code": f"import json; print(json.dumps({{'marker': {loop_marker!r}}}))",
        })
        loop = CognitiveLoop(model=ScriptedModel([completion]), broker=broker, principal_id="agent-a")
        loop_effect = loop.step("run the k8s thing")
        loop_intent_id = loop_effect.intent_id  # parse_intent() always mints this fresh

        # Internal: the SAME counting backend instance, now called a second time.
        assert loop_effect.execution_class == "k8s_pod"
        assert counting.call_count == 2
        assert loop_effect.detail["phase"] == "Succeeded"

        # External.
        loop_pods = _independent_pods_for_intent(loop_intent_id)
        assert len(loop_pods) == 1
        assert loop_pods[0]["status"]["phase"] == "Succeeded"
        assert loop_marker in _independent_logs(loop_pods[0]["metadata"]["name"])
    finally:
        delete_labeled_pods(label_value_for(direct_intent_id))
        if loop_intent_id is not None:
            delete_labeled_pods(label_value_for(loop_intent_id))


# ---- ALLOW / DENY via direct Broker.dispatch() (the external-harness-shaped caller) -----------

def test_deny_via_direct_dispatch_never_touches_kubernetes():
    gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}, allowed_kinds=("run_artifact",)))
    counting = _CountingK8sPodBackend(K8sPodBackend())
    executor = Executor(gate, backends={"k8s_pod": counting})
    broker = Broker(gate=gate, executor=executor)

    intent_id = f"k8s-deny-direct-{uuid.uuid4().hex[:8]}"
    out_of_scope = Intent(
        # "write_file" is not in allowed_kinds above -- Gate mints a real permitted=False Decision
        # whose execution_class is still "k8s_pod" (ConsequencePolicy derives execution_class from
        # `consequence` unconditionally -- see policy.py), so this exercises exactly the case
        # where the class that *would* have run is Kubernetes, and it still never runs.
        kind="write_file", principal_id="agent-a", intent_id=intent_id, consequence="k8s",
    )

    before = _independent_managed_pod_count()
    with pytest.raises(GateViolation):
        broker.dispatch(out_of_scope)

    # Internal: the backend itself was never invoked -- not inferred from the exception alone.
    assert counting.call_count == 0
    # External: a fresh cluster query, not a read of anything Siphonophore claimed (there is no
    # Effect to read -- dispatch() raised).
    assert _independent_pods_for_intent(intent_id) == []
    assert _independent_managed_pod_count() == before


# ---- DENY via CognitiveLoop (the reference-harness-shaped caller) -----------------------------

def test_deny_via_cognitive_loop_never_touches_kubernetes():
    """Weaker external evidence than test_deny_via_direct_dispatch_never_touches_kubernetes,
    deliberately: parse_intent() mints intent_id internally (uuid.uuid4()) and CognitiveLoop.step()
    never exposes it when GateViolation propagates before an Effect exists, so there is no
    intent-id label to query by. The before/after total-managed-Pod-count check below is the
    strongest available external signal, not a shortcut chosen for convenience -- it depends on
    nothing else changing the managed-Pod population between the two count calls, which is why
    delete_labeled_pods() (used by every other test in this file for cleanup) blocks until
    deletion actually completes rather than firing-and-forgetting."""
    gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}, allowed_kinds=("run_artifact",)))
    counting = _CountingK8sPodBackend(K8sPodBackend())
    executor = Executor(gate, backends={"k8s_pod": counting})
    broker = Broker(gate=gate, executor=executor)

    completion = json.dumps({"kind": "write_file", "consequence": "k8s"})
    loop = CognitiveLoop(model=ScriptedModel([completion]), broker=broker, principal_id="agent-a")

    before = _independent_managed_pod_count()
    with pytest.raises(GateViolation):
        loop.step("try something outside what's allowed")

    assert counting.call_count == 0
    assert _independent_managed_pod_count() == before
