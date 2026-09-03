# Kubernetes as an execution substrate (experimental)

Status: built and tested on `explore/k8s-substrate-demo`, not yet merged to `main`. This documents
what the `k8s_pod` execution class actually proves today, what it deliberately does not attempt,
and what a follow-on would need to add.

This slice went through one round of adversarial review before being treated as a checkpoint —
four real findings, all fixed here, not just noted: (1) an earlier test suite claimed "both direct
`Broker.dispatch()` and `CognitiveLoop.step()` reach the identical registered backend," but each
had wired its own separate `Executor`/backend instance, so what was actually proven was "the same
backend class, registered the same way, behaves consistently" — a materially weaker claim; fixed by
sharing one instance across both call shapes in one test
(`test_direct_dispatch_and_cognitive_loop_reach_the_identical_backend_instance`). (2) the DENY
test using `CognitiveLoop` had a strictly weaker external check (a before/after total-Pod-count
invariant, not a label-specific query) than its direct-dispatch counterpart, for a structural
reason (intent_id is lost when `GateViolation` propagates before an Effect exists) — this is now
stated explicitly in that test's own docstring rather than left to look equivalent. (3)
`delete_labeled_pods()` used `--wait=false`, which could race against a later test's before/after
Pod-count snapshot; it now blocks until deletion actually completes. (4) `K8sPodBackend.run()`
treated `exit_code is None` at phase `Succeeded` as a silent pass rather than a failure, and
indexed `containerStatuses[0]` positionally rather than by container name (wrong under sidecar
injection); both fixed to fail closed / resolve by name. The vocabulary-leakage check also gained
an AST-based identifier scan after review showed the original regex-only version would silently
pass a lowercase field like `namespace: str` added to a core dataclass — the actual realistic leak
shape, not the capitalized-class-name shape the regex alone could see.

Do not read this as "Siphonophore is now Kubernetes-native." Kubernetes is one concrete execution
substrate behind `ExecutionBackend` (`execution.py`), the same relationship `uid_cgroup` has to
Linux. Nothing about `Order`, `Authority`, `Intent`, `Decision`, `Gate`, `Executor`, `Broker`, or
`CognitiveLoop` changed or needed to change to add it — `tests/test_core_no_k8s_vocabulary.py`
makes that a checked property, not an assertion.

## What's proven

`K8sPodBackend` (`siphonophore_core/execution_k8s.py`) implements `ExecutionBackend` for execution
class `k8s_pod`: it renders `intent.artifact_code` into a Pod manifest, applies it via `kubectl`,
polls for a terminal phase, collects logs and exit code, and returns an `Effect`. Deliberately does
**not** delete the Pod on success — the same disclosed-not-fixed shape as `execution_uid_cgroup.py`'s
cgroup leaves, and useful here for the same reason: it's what lets an independent observer inspect
the real Pod after the fact. `delete_labeled_pods()` is the explicit, separate cleanup path.

Proven end-to-end on a local `kind` cluster (`tests/test_harness_loop_k8s_cluster.py`, marker
`k8s_cluster`):

- **ALLOW**: a permitted dispatch actually creates and runs a real Pod. Checked internally
  (`K8sPodBackend.run()` was invoked exactly once; the returned `Effect`'s `phase`/`exit_code`) and
  externally (a *separate* `kubectl get`/`kubectl logs` call the test makes on its own, using only
  the intent_id as a correlation key — never reading the Pod's actual state off the `Effect`).
- **DENY**: a refused dispatch never reaches Kubernetes at all. Checked internally (the backend's
  `run()` is never invoked — asserted via a call-counting wrapper, not inferred from the absence of
  an exception) and externally (a fresh cluster query finds no corresponding Pod).
- **Both `Broker.dispatch()` called directly and `CognitiveLoop.step()`** (a real model-produced
  completion, parsed by `intent_parsing.py`, not a directly-constructed `Intent`) reach the
  identical registered `K8sPodBackend` — the reference-harness/external-harness distinction the
  design review asked for, demonstrated rather than assumed.

`tests/test_execution_k8s_cluster.py` exercises the backend directly (below the Gate/Broker layer)
as a smoke test. `tests/test_execution_k8s.py` covers the portable naming helpers (`pod_name_for`,
`label_value_for`) with no cluster required.

## What this deliberately does not attempt

Named explicitly per the design review's instruction not to add these unless implementation
revealed them as strictly necessary — it didn't:

- **No Kubernetes check-in / identity-binding tier.** `uid_cgroup_checkin` independently confirms
  execution identity through the kernel (`SO_PEERCRED`) before anything self-reported is trusted;
  `k8s_pod` has no equivalent. A real k8s "checked-in" tier would need something that plugs into
  `identity.py`'s `CheckinRegistry.handle_checkin(presented_nonce, peer_uid)` contract with a
  k8s-appropriate notion of verified peer identity — e.g. verifying a Pod's projected
  service-account token, or an admission-time attestation. `CheckinRegistry`'s own logic is already
  portable and needs no change; only a new listener/verifier analogous to
  `CheckinListener`/`read_peer_uid` (`identity.py:157-228`) would be new code. Not built here.
- **No pluggable ground-truth-observer interface.** `audit.py`'s `collect_ground_truth()` is
  hardcoded to a local directory listing; `reconcile()`/`reconcile_path()` themselves take
  already-produced booleans/content, so nothing prevents a k8s-specific ground-truth source, but
  no shared "pluggable observer" abstraction exists to plug one into. This slice's own "external"
  verification is a fresh `kubectl` call made directly in the test, not a reusable observer
  component.
- **No AgentWatch integration.** AgentWatch (a sibling project) is explicitly not a Siphonophore
  dependency and stays external — nothing here imports or invokes it. The independent verification
  this slice does is a stand-in for what an AgentWatch-based observer would do from its own
  audit-log/eBPF vantage; wiring AgentWatch itself in as an actual second observer is real,
  unstarted follow-on work.
- **No managed-cloud cluster.** Proven against `kind` only. The same architecture is expected to
  survive a real managed cluster (EKS/AKS) unchanged at the `ExecutionBackend` boundary, but that's
  an expectation, not something this slice tested.

## Where a place proved genuinely new, not just "fill in the abstraction"

The `ExecutionBackend` abstraction itself needed no change — the extension point worked exactly as
`execution.py`'s own docstring described. Two things had no existing precedent to reuse, both
resolved locally inside `execution_k8s.py` rather than by changing anything shared:

- **Execution-id-to-substrate-name mapping.** Every existing backend treats `decision.intent_id`
  as its own execution_id, validated against that backend's own naming rules
  (`execution_uid_cgroup.py`'s `_EXECUTION_ID_RE`, a cgroup-directory-safe charset). A Kubernetes
  Pod name has stricter rules (RFC 1123 DNS label, must be cluster-unique) that an arbitrary
  `intent_id` won't already satisfy, so `pod_name_for()` derives a compliant, collision-safe name
  rather than validating and rejecting like the uid_cgroup backends do. This is a real difference in
  how "the same execution_id" gets projected onto a substrate's naming rules — each backend already
  owned this independently, and `k8s_pod` needing its own version (rather than sharing
  `_EXECUTION_ID_RE`) is consistent with, not a break from, that existing pattern.
- **After-the-fact correlation without a live channel.** The uid_cgroup tiers establish identity
  synchronously, inline in `run()` (a pipe handshake, a check-in socket). Kubernetes offers no
  equivalent live channel to this backend's own `kubectl` calls; correlation instead relies on a
  label (`siphonophore.dev/intent-id`) an independent observer can query after the fact. This is
  what made not-deleting the Pod on success load-bearing rather than merely tolerated (see above) —
  without a live channel, the Pod's continued existence *is* the evidence trail.

Neither of these needed a new abstraction in `execution.py`, `mediation.py`, or `policy.py` — both
are backend-local, the same way `uid_cgroup`'s own naming and identity mechanics are backend-local.
