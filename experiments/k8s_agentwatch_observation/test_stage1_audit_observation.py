"""Stage 1: Siphonophore <-> AgentWatch Kubernetes-audit-log observation EXPERIMENT.

Experiment, not a demo, not routine regression coverage -- see README.md for the full protocol,
evidentiary-category definitions, and results. Not collected by a bare `pytest` run from the repo
root (pyproject.toml's `testpaths` is `tests/` only); run explicitly:

    python3 experiments/k8s_agentwatch_observation/setup_cluster.py   # once, idempotent
    cd experiments/k8s_agentwatch_observation
    ../../.venv/bin/python -m pytest test_stage1_audit_observation.py -v

Requires a sibling AgentWatch checkout reachable via the AGENTWATCH_REPO env var (default
~/dev/agentwatch) -- a hard machine dependency, allowed to fail loudly at import time exactly like
a missing `kubectl` does elsewhere in this repo, not silently skipped.

THE EXPERIMENTAL QUESTION (see README.md): can AgentWatch's existing, unmodified Kubernetes
audit-log observation machinery independently observe and correlate a Kubernetes execution
Siphonophore claims to have mediated, without integrating AgentWatch into Siphonophore or changing
Siphonophore's execution semantics? K8sPodBackend is used completely unmodified here, pointed at a
second, audit-configured cluster through its EXISTING `context=` parameter.

EVIDENTIARY CATEGORIES kept explicitly separate throughout (see README.md for the full
definitions) -- every assertion below is labeled by which one it belongs to:
  1. SIPHONOPHORE EVIDENCE      -- Decision / dispatch / backend invocation / Effect
  2. KUBERNETES AUDIT EVIDENCE  -- an API-server-level record (principal, verb, resource, result);
                                    does NOT prove a container process actually ran
  3. KUBERNETES LIVE OBJECT STATE -- what the API currently reports about a Pod's lifecycle
  (4. eBPF/kernel observation is explicitly NOT part of Stage 1)
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid

import pytest
from correlate import pods_create_events_in_namespace_window, read_audit_events, wait_for_audit_events
from setup_cluster import KUBE_CONTEXT

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

NAMESPACE = "default"
# Buffer between a DENY attempt and reading "absence" from either external channel -- guards the
# temporal race named explicitly in the work order: concluding absence before a write that WOULD
# have appeared has had time to land would be a false negative dressed up as a finding. Adversarial
# review flagged this as an unmeasured guess in an earlier version -- measured directly against
# this same cluster (kubectl create -> the create event visible via read_audit_events()): 5 runs,
# worst case 0.28s, typical ~0.05s. 3.0s is roughly 10x the observed worst case, not an arbitrary
# round number.
ABSENCE_SETTLE_SECONDS = 3.0


def _preconditions_met() -> bool:
    try:
        require_cluster_reachable(context=KUBE_CONTEXT)
    except ProvisioningError:
        return False
    try:
        import correlate

        correlate.AUDIT_LOG_PATH.read_text()
    except OSError:
        return False
    return True


requires_experiment_cluster = pytest.mark.skipif(
    not _preconditions_met(),
    reason="needs `python3 setup_cluster.py` run first (a second, audit-configured kind cluster)",
)
pytestmark = requires_experiment_cluster


class _CountingK8sPodBackend(ExecutionBackend):
    """Same instrumentation as tests/test_harness_loop_k8s_cluster.py: counts run() invocations so
    a DENY case can assert Category-1 non-invocation directly, not merely infer it from the
    exception."""

    def __init__(self, real: K8sPodBackend) -> None:
        self._real = real
        self.call_count = 0

    def run(self, decision, intent) -> Effect:
        self.call_count += 1
        return self._real.run(decision, intent)


def _wiring():
    gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}))
    counting = _CountingK8sPodBackend(K8sPodBackend(context=KUBE_CONTEXT))
    executor = Executor(gate, backends={"k8s_pod": counting})
    broker = Broker(gate=gate, executor=executor)
    return gate, counting, broker


def _managed_pod_count() -> int:
    result = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "get", "pods",
         "-l", "app.kubernetes.io/managed-by=siphonophore", "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    return len(json.loads(result.stdout)["items"])


# ---- ALLOW ----------------------------------------------------------------------------------

def test_allow_siphonophore_dispatch_independently_observed_in_k8s_audit_log():
    """ALLOW acceptance criteria (see README.md):
    A. Siphonophore's own Decision/dispatch produces a permitted mediation path and a successful
       Effect.
    B. K8sPodBackend's OWN existing live-state checks (status.phase / containerStatuses exitCode)
       report success -- Siphonophore's self-report about Kubernetes live state, still category 1.
    C. AgentWatch's k8s_audit.parse_lines(), reading the audit log independently, produces a
       successful `pods` CREATE event for the SAME concrete Pod name Siphonophore's Effect
       reports -- category 2, and the actual facts asserted (verb, success, resource_id) are read
       entirely from the independently-tailed log, never from `effect`.
    D. Each block below is labeled by evidentiary category."""
    _gate, counting, broker = _wiring()
    intent_id = f"stage1-allow-{uuid.uuid4().hex[:8]}"
    marker = f"marker-{uuid.uuid4().hex[:8]}"
    intent = Intent(
        kind="run_artifact", principal_id="agent-a", intent_id=intent_id, consequence="k8s",
        artifact_code=f"import json; print(json.dumps({{'marker': {marker!r}}}))",
    )
    try:
        # ---- CATEGORY 1: Siphonophore-internal (Decision, dispatch, Effect) ----
        effect = broker.dispatch(intent)
        assert effect.execution_class == "k8s_pod"
        assert counting.call_count == 1
        pod_name = effect.detail["pod_name"]

        # ---- Criterion B: K8sPodBackend's own existing live-state checks (still category 1 --
        # Siphonophore's self-report, even though its underlying mechanism is a K8s API read) ----
        assert effect.detail["phase"] == "Succeeded"
        assert effect.detail["exit_code"] == 0

        # ---- CATEGORY 2: Kubernetes AUDIT evidence, independently parsed by AgentWatch's code ----
        # pod_name used only as the correlation KEY -- explicitly sanctioned (independence means
        # the evidence comes from AgentWatch's own observation source, not that AgentWatch must be
        # ignorant of what to look for).
        audit_events = wait_for_audit_events(
            lambda e: e.args == ("create", f"pods:{NAMESPACE}/{pod_name}") and e.success is True,
            timeout=15.0,
        )
        assert len(audit_events) == 1, f"expected exactly one create-success audit event for {pod_name!r}, got {audit_events}"
        audit_event = audit_events[0]
        assert audit_event.args == ("create", f"pods:{NAMESPACE}/{pod_name}")
        assert audit_event.success is True
        # This audit event proves an API-server-level fact ONLY: a `pods create` request for this
        # object was accepted (response code < 400). It does NOT prove any container process
        # inside the resulting Pod ever ran -- that's what criterion B established separately, via
        # a different mechanism (kubelet-reported status, not an API audit record). Do not read
        # this assertion as kernel-level or process-level confirmation.
    finally:
        delete_labeled_pods(label_value_for(intent_id), context=KUBE_CONTEXT)


# ---- DIRECT-DISPATCH DENY --------------------------------------------------------------------

def test_deny_direct_dispatch_internal_and_external_absence():
    """DIRECT-DISPATCH DENY: the caller supplies intent_id before dispatch, so the would-be
    intent-derived label is known in advance -- the STRONGEST live-object-state absence check
    available (category 3) is therefore label-specific. The audit-side absence check (category 2)
    stays windowed regardless of that: k8s_audit.py never reads labels (confirmed from source), and
    a denied dispatch never reaches K8sPodBackend.run() (so pod_name_for() is never even called) --
    there is no name to filter audit events by either. Both facts hold independent of whether the
    id was precomputable."""
    gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}, allowed_kinds=("run_artifact",)))
    counting = _CountingK8sPodBackend(K8sPodBackend(context=KUBE_CONTEXT))
    executor = Executor(gate, backends={"k8s_pod": counting})
    broker = Broker(gate=gate, executor=executor)

    intent_id = f"stage1-deny-direct-{uuid.uuid4().hex[:8]}"
    label = label_value_for(intent_id)
    out_of_scope = Intent(kind="write_file", principal_id="agent-a", intent_id=intent_id, consequence="k8s")

    window_start = time.time()
    with pytest.raises(GateViolation):
        broker.dispatch(out_of_scope)
    time.sleep(ABSENCE_SETTLE_SECONDS)
    window_end = time.time()

    # ---- CATEGORY 1: Siphonophore-internal -- non-invocation asserted directly ----
    assert counting.call_count == 0

    # ---- CATEGORY 3: Kubernetes LIVE OBJECT STATE, independently queried, label-specific ----
    # A CURRENT-state absence claim ("no Pod with this exact label exists right now"), not by
    # itself an eternal "never existed" claim -- see README.md's temporal-semantics note. Combined
    # with category 1's stronger guarantee (the backend was never invoked during this run at all),
    # the honest scoped claim this test supports is "no such Pod was ever created during this
    # test's own execution" -- not an unbounded claim about all time.
    live = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "get", "pods",
         "-l", f"siphonophore.dev/intent-id={label}", "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(live.stdout)["items"] == []

    # ---- CATEGORY 2: Kubernetes AUDIT evidence -- windowed + namespace-scoped, NOT name-specific ----
    # Deliberately the coarser of the two absence checks even though intent_id was known in
    # advance: the audit channel's absence claim is bounded by TIME WINDOW + NAMESPACE, not by
    # object identity, for a structural reason (the parser never reads labels) unrelated to
    # whether an id was precomputable.
    events = read_audit_events()
    hits = pods_create_events_in_namespace_window(events, NAMESPACE, window_start, window_end)
    assert hits == [], f"expected no pods-create audit activity in {NAMESPACE!r} during the attempt window, found {hits}"


# ---- COGNITIVELOOP DENY -----------------------------------------------------------------------

def test_deny_via_cognitive_loop_windowed_absence_only():
    """COGNITIVELOOP DENY: intent_id is minted internally by parse_intent() and lost when
    GateViolation propagates before an Effect exists -- there is no name or label to check against
    in EITHER external channel. Both the live-state check (category 3) and the audit check
    (category 2) reduce to windowed, namespace-scoped absence claims -- weaker than the
    direct-dispatch case's category-3 check specifically. This asymmetry is preserved deliberately,
    not manufactured away, per the work order: no identifier is invented merely for test symmetry."""
    gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}, allowed_kinds=("run_artifact",)))
    counting = _CountingK8sPodBackend(K8sPodBackend(context=KUBE_CONTEXT))
    executor = Executor(gate, backends={"k8s_pod": counting})
    broker = Broker(gate=gate, executor=executor)
    completion = json.dumps({"kind": "write_file", "consequence": "k8s"})
    loop = CognitiveLoop(model=ScriptedModel([completion]), broker=broker, principal_id="agent-a")

    before_count = _managed_pod_count()
    window_start = time.time()
    with pytest.raises(GateViolation):
        loop.step("try something outside what's allowed")
    time.sleep(ABSENCE_SETTLE_SECONDS)
    window_end = time.time()

    # ---- CATEGORY 1 ----
    assert counting.call_count == 0

    # ---- CATEGORY 3 -- windowed/global, NOT label-specific (no id survives the exception) ----
    assert _managed_pod_count() == before_count

    # ---- CATEGORY 2 -- windowed, namespace-scoped, structurally identical shape to the
    # direct-dispatch case's audit check (both are bounded by time+namespace, not identity) ----
    events = read_audit_events()
    hits = pods_create_events_in_namespace_window(events, NAMESPACE, window_start, window_end)
    assert hits == [], f"expected no pods-create audit activity in {NAMESPACE!r} during the attempt window, found {hits}"

    # The strongest honest statement this test supports, stated exactly (see README.md):
    # "During this test's own attempt window, no qualifying pods-create API activity was observed
    # in the `default` namespace, and no new Siphonophore-managed Pod appeared." NOT: "this
    # specific denied intent never ran" -- there is no identifier left to make that claim about.
