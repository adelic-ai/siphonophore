# Kubernetes as an execution substrate

**This is the substrate layer.** Kubernetes vocabulary — Pods, namespaces, ServiceAccounts,
kubeconfigs, `kubectl`, `kind` — belongs here and only here. Nothing in this document redefines a
Siphonophore core concept, and nothing here should be read upward: `Order`, `Authority`, `Intent`,
`Decision`, `Gate`, `Executor`, `Broker`, and `CognitiveLoop` mean exactly what `DESIGN.md` says they
mean, unchanged by anything below.

The distinction to hold onto, because both halves are true at once:

> **The Kubernetes implementation is genuinely Kubernetes-native. Siphonophore itself remains
> Kubernetes-independent.**

Kubernetes is a real, first-class execution substrate behind `ExecutionBackend` — the same
relationship the `uid_cgroup` classes have to Linux, and the substrate that first confirmed the
boundary holds for a second, differently-shaped implementation. It is not what Siphonophore is.
`tests/test_core_no_k8s_vocabulary.py` makes that a checked property rather than an assertion; see
`DESIGN.md` §10 for the invariant itself, and [`EXECUTION.md`](EXECUTION.md) for the
substrate-neutral model this document is one realization of.

## Status

- **`k8s_pod` backend** — implemented and on `main` (`siphonophore_core/execution_k8s.py`). Proven
  end-to-end against a local `kind` cluster. Not part of the default `Policy` mapping: a deployment
  registers it explicitly, exactly as the `uid_cgroup` tiers are registered explicitly.
- **No check-in / identity-binding tier** — `k8s_pod` has no equivalent of `uid_cgroup_checkin`. It
  is therefore the substrate with the *weaker* execution-identity story today, not the stronger one.
- **Independent observation** — a completed two-stage experiment established that a Siphonophore-mediated
  Kubernetes execution can be independently observed and correlated from outside Siphonophore's trust
  domain, on one tested topology. Methods, evidence categories, and limits:
  [`../experiments/k8s_agentwatch_observation/README.md`](../experiments/k8s_agentwatch_observation/README.md).
  That is a correlation result about executions that happened; it is not a claim about path
  exclusivity, and none is made here.
- **Mediation-bypass study** — pre-registered and implemented on the `explore/k8s-mediation-bypass`
  branch. The study has been executed and closed; publication of its final scientific record into
  that branch's own documentation is a separate, not-yet-done integration task — the branch's
  committed README still reads "PRE-REGISTERED DESIGN — IMPLEMENTED, NOT EXECUTED," which is stale
  relative to the study's actual execution status. Its subject is the deployment half of `DESIGN.md`
  §10's split — substrate-authority custody — not a property of this backend, so no specific result
  from it is asserted in this or any other canonical document pending that publication.
- **Managed clusters** — untested. `kind` only.

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
- **DENY**: a refused dispatch does not reach the backend, and no corresponding Pod is found.
  Checked internally (the backend's `run()` is never invoked — asserted via a call-counting wrapper,
  not inferred from the absence of an exception) and externally (a fresh cluster query finds no
  corresponding Pod). Note the asymmetry `DESIGN.md` §10 names: the internal check is direct
  evidence of non-invocation, while the external check is a **current-state absence**, not a proof
  that no such Pod ever existed.
- **Both `Broker.dispatch()` called directly and `CognitiveLoop.step()`** (a real model-produced
  completion, parsed by `intent_parsing.py`, not a directly-constructed `Intent`) reach the
  identical registered `K8sPodBackend` — the reference-harness/external-harness distinction the
  design review asked for, demonstrated rather than assumed.

`tests/test_execution_k8s_cluster.py` exercises the backend directly (below the Gate/Broker layer)
as a smoke test. `tests/test_execution_k8s.py` covers the portable naming helpers (`pod_name_for`,
`label_value_for`) with no cluster required.

## What Siphonophore supplies here, and what the deployment must

`K8sPodBackend` is below the substrate boundary, and that has concrete consequences for this
substrate specifically. They are stated here rather than left to be discovered from the code:

- **The backend performs no authorization check, by design.** `Executor.execute()` has already
  verified decision↔intent correspondence, the `Decision`'s HMAC, `permitted`, and the artifact
  digest before the backend is looked up at all. `K8sPodBackend.run()` reads exactly one field off
  the `Decision` — `decision.intent_id`, used to derive a Pod name (`pod_name_for()`) and a
  correlation label (`label_value_for()`) — and never calls `gate.verify()`, never reads `permitted`,
  never recomputes the digest. Decision authenticity is an `Executor`/`Gate`
  property; do not describe it as something this backend establishes.
- **The backend holds no Kubernetes credential.** Its constructor takes `namespace`, `image`,
  `kubectl`, `context`, `timeout`, `poll_interval` — no token, no kubeconfig path. It shells out to
  `kubectl`, which resolves credentials from ambient process state (`$KUBECONFIG`, `~/.kube/config`,
  or an in-cluster ServiceAccount token). **The substrate authority used here is whatever authority
  the calling process already holds.** This is `EXECUTION.md`'s general "ambient credentials are
  whatever the executing process already happens to hold" as a concrete instance.
- **Therefore the deployment, not Siphonophore, decides who can reach the cluster.** Which Unix
  identity runs the mediating process, where its kubeconfig lives, what RBAC that identity has, and
  whether any other identity on the host holds an equivalent credential are all deployment
  properties. `DESIGN.md` §10 names this split (authorization semantics versus substrate-authority
  custody); the reason it matters most visibly on this substrate is that a `kubectl` binary plus a
  readable kubeconfig is a complete alternative path to the same effect, and no library-side code can
  remove it.
- **The deployment also decides what any observer can see.** API-server audit policy — what is
  recorded, at what level, for which resources — is cluster configuration. Siphonophore does not
  request it, require it, or verify it, and an observation's strength is bounded by it.

### Workload-side Kubernetes identity

Stated precisely, and deliberately not generalized: **`K8sPodBackend` sets no `serviceAccountName` on
the Pods it creates.** A mediated Pod therefore runs under its namespace's `default` ServiceAccount,
with a projected token automounted according to the cluster's own defaults. Siphonophore neither
supplies nor scopes that credential.

This is a Kubernetes engineering consideration, not a platform-independent core concept. "What
credential the executed workload itself holds" is the Credentials dimension `EXECUTION.md` names as
unbuilt on *every* substrate — Siphonophore ties no credential delivery to an authorized execution
anywhere today. Whether `automountServiceAccountToken: false` should become a backend default, be
left to deployment configuration, or surface as a policy-visible field is recorded as open in
`DESIGN.md` and is not decided here.

### Correlation on this substrate

There is no live identity channel between backend and workload here — no `SO_PEERCRED` equivalent, no
handshake. Correlation is after-the-fact, through the `siphonophore.dev/intent-id` label an
independent observer can select on, which is what makes not deleting the Pod on success load-bearing
rather than merely tolerated. Correlating records from several vantages to one concrete Pod is
correlation; it is not mediation, and it does not establish that the execution had to occur through
Siphonophore (`DESIGN.md` §10).

## What this deliberately does not attempt

Named explicitly per the design review's instruction not to add these unless implementation
revealed them as strictly necessary — it didn't:

- **No Kubernetes check-in / identity-binding tier — open design, not a chosen mechanism.**
  `uid_cgroup_checkin` independently confirms execution identity through the kernel (`SO_PEERCRED`)
  before anything self-reported is trusted; `k8s_pod` has no equivalent, and none is built here. The
  Linux mechanism is prior art, not a template to port: the substrate-neutral question — what
  execution-identity property should a Kubernetes substrate establish, and by what independent
  verification — has to be answered before any Kubernetes-specific mechanism is chosen. `identity.py`'s
  `CheckinRegistry.handle_checkin(presented_nonce, peer_uid)` is one existing, already-portable
  contract that a future listener could plug into, and a Pod's projected service-account token or an
  admission-time attestation are candidate k8s-side verification sources — named here as possibilities
  under consideration, not as a decided design. Nothing in this repository commits Kubernetes to
  reusing the Linux check-in protocol specifically.
- **No pluggable ground-truth-observer interface — open design, not a planned addition.** `audit.py`'s
  `collect_ground_truth()` is hardcoded to a local directory listing; `reconcile()`/`reconcile_path()`
  themselves take already-produced booleans/content, so nothing in the code prevents a k8s-specific
  ground-truth source from existing, but no shared "pluggable observer" abstraction exists in core,
  and building one is not a settled direction. This slice's own "external" verification is a fresh
  `kubectl` call made directly in the test, not a reusable observer component. Independent observation
  can strengthen assurance without becoming part of Siphonophore's authorization trust path — that is
  the invariant (`DESIGN.md` §5); whether it should also gain a shared interface *in* core is a
  separate, unresolved question this repository does not answer by having built one experimentally.
- **No AgentWatch integration.** AgentWatch (a sibling project) is explicitly not a Siphonophore
  dependency and stays external — nothing here imports or invokes it. The independent verification
  this slice does is a stand-in for what an AgentWatch-based observer would do from its own
  audit-log/eBPF vantage. Using AgentWatch itself as an actual second observer has since been
  done — as a separate, out-of-tree experiment that changed neither this backend nor AgentWatch,
  and that deliberately did not make AgentWatch a dependency; see
  [`../experiments/k8s_agentwatch_observation/README.md`](../experiments/k8s_agentwatch_observation/README.md).
  Nothing in this slice imports or invokes AgentWatch, and no authorization decision anywhere
  depends on an AgentWatch observation.
- **No managed-cloud cluster.** Proven against `kind` only. The same architecture is expected to
  survive a real managed cluster (EKS/AKS) unchanged at the `ExecutionBackend` boundary, but that's
  an expectation, not something this slice tested.
- **No claim of path exclusivity.** Nothing here establishes that a Kubernetes execution could not
  have been produced without going through Siphonophore, and nothing here could: a process holding a
  usable kubeconfig reaches the API server directly, without entering this code. Whether a given
  deployment closes that path is a credential-custody question about that deployment
  (`DESIGN.md` §10).

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

## Review history

Preserved because the corrections are the reason several of the statements above are worded as
narrowly as they are. This slice went through one round of adversarial review before being treated
as a checkpoint — four real findings, all fixed, not just noted:

1. An earlier test suite claimed "both direct `Broker.dispatch()` and `CognitiveLoop.step()` reach
   the identical registered backend," but each had wired its own separate `Executor`/backend
   instance, so what was actually proven was "the same backend class, registered the same way,
   behaves consistently" — a materially weaker claim. Fixed by sharing one instance across both call
   shapes in one test (`test_direct_dispatch_and_cognitive_loop_reach_the_identical_backend_instance`).
2. The DENY test using `CognitiveLoop` had a strictly weaker external check (a before/after
   total-Pod-count invariant, not a label-specific query) than its direct-dispatch counterpart, for a
   structural reason: `intent_id` is lost when `GateViolation` propagates before an `Effect` exists.
   Now stated explicitly in that test's own docstring rather than left to look equivalent.
3. `delete_labeled_pods()` used `--wait=false`, which could race against a later test's before/after
   Pod-count snapshot; it now blocks until deletion actually completes.
4. `K8sPodBackend.run()` treated `exit_code is None` at phase `Succeeded` as a silent pass rather
   than a failure, and indexed `containerStatuses[0]` positionally rather than by container name
   (wrong under sidecar injection); both fixed to fail closed / resolve by name.

The vocabulary-leakage check also gained an AST-based identifier scan after review showed the
original regex-only version would silently pass a lowercase field like `namespace: str` added to a
core dataclass — the actual realistic leak shape, not the capitalized-class-name shape the regex
alone could see.
