# Stage 1: Siphonophore ↔ AgentWatch Kubernetes audit-log observation

**Status: experiment, not a demo, not product work.** This directory is not part of Siphonophore's
core, is not a dependency edge from Siphonophore to AgentWatch, and is not meant to be polished for
users. It exists to answer one question with real evidence.

## The experimental question

Can AgentWatch's existing Kubernetes observation machinery independently observe and correlate a
Kubernetes execution that Siphonophore claims to have mediated, without integrating AgentWatch into
Siphonophore or changing Siphonophore's execution semantics?

**Result: yes, for the audit-log leg (Stage 1).** See Results below.

## Prerequisites (external to this repo)

Verified against these exact versions/revisions — recorded here as a portability checkpoint after
a housekeeping/reproducibility review (2026-09-04), not as a hard-enforced requirement:

- **`kind`** v0.33.0, default node image for that version (`kindest/node:v1.37.0`) — not pinned in
  `kind/kind-config.yaml.tmpl`; a different kind/node-image version is expected to work
  identically for this experiment's purposes (`audit.k8s.io/v1` JSON has been stable for years),
  but hasn't itself been tested.
- **`kubectl`** v1.37.0, and **Docker** (Docker Desktop on macOS during development; a plain Linux
  Docker Engine is expected to work identically for cluster creation and `docker exec` — this
  experiment never uses `kind load docker-image`, which is the one command that hit a real
  Docker-Desktop-specific containerd-image-store bug during the earlier `kind-siphonophore-demo`
  setup; that bug is therefore not expected to reproduce here regardless).
- **Outbound network access** from wherever the kind node's containerd runs, to pull
  `python:3.12-slim` from Docker Hub — not vendored or pre-loaded. An offline/restricted-egress
  build environment would need this image pre-loaded (`kind load docker-image` or equivalent) or
  a local registry mirror; neither is set up here.
- **A sibling AgentWatch checkout.** See "AgentWatch checkout and revision" below — the one
  genuine cross-repo dependency this experiment has, and it is not a Siphonophore package
  dependency (no `pip install`, no `pyproject.toml` entry — confirmed by inspection: the only
  AgentWatch modules ever imported, `agentwatch.events` and `agentwatch.groundtruth.k8s_audit`,
  have an entirely stdlib-only import chain — `json`, `typing`, `dataclasses`, `datetime` — so
  nothing from AgentWatch's own dependency set needs installing either).
- **Siphonophore's own existing Python environment** — the top-level `README.md`'s standard
  `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` (this experiment adds no
  requirement beyond that; `pytest` and the `siphonophore_core`/`siphonophore_harness` packages
  are all it needs from the venv).

## AgentWatch checkout and revision

`correlate.py` reaches AgentWatch via `sys.path` injection to a sibling checkout, path
configurable with the `AGENTWATCH_REPO` env var (default `~/dev/agentwatch`) — this is
deliberately not a Siphonophore dependency (no entry anywhere in `pyproject.toml`) and this
experiment does not turn it into one; it is a reference to another repository's files on disk, the
same relationship any external tool consuming AgentWatch's code would have.

Actually verified against AgentWatch commit `92037e9ee926ce817829d34923b914b93c16f152` (branch
`docs/adapter-currency-caveat`, 2026-08-31), clean working tree. `agentwatch.groundtruth.k8s_audit`
is a small, self-contained, stdlib-only parser (56 lines of actual logic; its full source is
reproduced and cited by file:line throughout this README's "Why Metadata-level auditing" section)
— it is not expected to be revision-sensitive in any way that would break this experiment, but no
later revision has actually been tested. If AgentWatch's `parse_lines()` contract changes (the
`GroundTruthEvent(comm, args=(verb, resource_id), success, ...)` shape this experiment depends on
in `correlate.py`), this experiment's tests would fail loudly (an `AttributeError`/`AssertionError`
against real data), not silently produce wrong results — no version pin is enforced in code because
none previously existed in this repo's own convention for cross-repo dev-tool checkouts, and adding
one (e.g. a git submodule) would be exactly the kind of architectural change this housekeeping pass
was told not to make.

## Scope: Stage 1 only

This is the K8s-audit-log leg only. eBPF/kernel observation (Stage 2) is explicitly not attempted
here — see "What Stage 1 does not establish." (Update 2026-09-04: the specific feasibility question
of whether host-level eBPF observation *can* reach a K8s Pod at all — as opposed to a full Stage 2
implementation — has since been answered by a separate, later probe; see "Stage 2 precursor" below.
Stage 1 itself, as documented in this section and its own results, still establishes nothing at the
kernel level — that has not changed.)

## Why a second cluster, not `kind-siphonophore-demo`

kind bakes audit-policy wiring into the API server at `kubeadm init` time (`kubeadmConfigPatches` →
`ClusterConfiguration.apiServer.extraArgs`/`extraVolumes`), confirmed by reading AgentWatch's own
`demo/k8s/kind-config.yaml` and cross-checking against kind's own bootstrap model — not something
addable to an already-running cluster without unsupported static-control-plane-manifest surgery.
`kind-siphonophore-demo` (the cluster the committed `K8sPodBackend` vertical slice already uses) was
created without this. Rather than touch it, this experiment stands up a second cluster,
`sipho-agentwatch-audit` (`kubectl context: kind-sipho-agentwatch-audit`), via
`setup_cluster.py`, and points the *existing, unmodified* `K8sPodBackend` at it through its
already-existing `context=` constructor parameter. **Zero Siphonophore code changes were required
for this** — confirmed by the diff (`git diff --stat siphonophore_core siphonophore_harness` is
empty for this entire work unit).

## Why Metadata-level auditing, not RequestResponse

Read `agentwatch/groundtruth/k8s_audit.py` directly: `parse_lines()` builds every event from
`objectRef` (`resource`/`namespace`/`name`), `user.username`, `verb`, and `responseStatus.code` —
it never reads `requestObject`/`responseObject` (confirmed: no such key appears anywhere in the
file). `RequestResponse`-level auditing (full object body, including `metadata.labels`) buys nothing
this parser can use. `kind/audit-policy.yaml` here captures `pods` at `level: Metadata` only, and
drops everything else (`level: None`) — narrower than AgentWatch's own demo policy, which also
captures `configmaps`/`secrets` at `RequestResponse` for its own (different) demo. This experiment's
policy is scoped to exactly what it tests.

## AgentWatch usage boundary

The **only** AgentWatch import anywhere in this experiment is
`agentwatch.groundtruth.k8s_audit.parse_lines` (`correlate.py`), used completely unmodified — the
low-level, standalone audit-log parser. Confirmed by reading its source: no Warrant, no
`GrantEvent`, no `subject_id` concept anywhere in its call graph.

**Not used, deliberately:** `agentwatch.reconciler.k8s_identity.IdentityCorrelator` and
`agentwatch.reconciler.k8s_scope` are built around a stated demo convention —
`IdentityCorrelator`'s own docstring: *"the K8s ServiceAccount's name IS the Warrant subject_id,
provisioned identically on purpose."* Siphonophore's Pods run under the `default` ServiceAccount
(no `serviceAccountName` is set anywhere in `K8sPodBackend`) and there is no Warrant, no
`GrantEvent`, and no per-execution ServiceAccount distinction here. Running Siphonophore's
operator-issued audit events through `IdentityCorrelator` would just always resolve to
`subject_id=None`. This experiment does not manufacture a Warrant-shaped identity to make those
modules apply — it consumes the standalone parser directly and does correlation with new,
experiment-only glue (`correlate.py`), living in neither project's own package.

AgentWatch's own repo is untouched by this experiment (reached only via `sys.path` injection to a
sibling checkout — no AgentWatch file was ever edited).

## Evidentiary categories (kept separate throughout — see the code's own comments for exactly where each applies)

1. **Siphonophore evidence** — `Decision`, `dispatch`, backend invocation/non-invocation, `Effect`.
   Siphonophore-internal claims. Includes `K8sPodBackend`'s own live-state poll
   (`status.phase`/`containerStatuses[].terminated.exitCode`) — that poll's *mechanism* touches the
   Kubernetes API, but it is reported here as Siphonophore's own self-claim, not as independently
   re-observed evidence — see criterion B below.
2. **Kubernetes audit evidence** — an API-server-level record: principal (`user.username`), verb,
   resource/object, and the API server's own response code. **Does not establish that a container
   process actually executed** — `k8s_audit.py`'s own docstring: *"a K8s audit event has no
   process-tree shape — it's a control-plane API call, not a syscall."* Never described here as
   process-execution ground truth.
3. **Kubernetes live object state** — what the API currently reports about a Pod's lifecycle
   (`status.phase`, `containerStatuses`). Different from an audit event (a live query, not a log of
   past requests). A live query returning nothing is a **current-state** absence claim, not proof a
   matching Pod never existed — kept explicit throughout, especially in the DENY cases.
4. **eBPF/kernel observation** — **not part of Stage 1.** Not implemented, not simulated, not
   approximated by anything here.

## Reproducing

Assumes the top-level `README.md`'s standard environment is already set up
(`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"` from the repo root) — see
"Prerequisites" above for everything else (kind, kubectl, Docker, a sibling AgentWatch checkout).

```
python3 experiments/k8s_agentwatch_observation/setup_cluster.py   # once, idempotent
cd experiments/k8s_agentwatch_observation
AGENTWATCH_REPO=~/dev/agentwatch ../../.venv/bin/python -m pytest test_stage1_audit_observation.py -v
python3 teardown_cluster.py   # explicit, separate -- never automatic
```

Not collected by a bare `pytest` run from the repo root — `pyproject.toml`'s `testpaths` is
`tests/` only; this directory is deliberately outside the regression suite.

## Results

Run against a real `kind` cluster (`sipho-agentwatch-audit`) and a real sibling AgentWatch checkout,
using its actual, unmodified `k8s_audit.parse_lines()`. All three tests passed.

**ALLOW** (`test_allow_siphonophore_dispatch_independently_observed_in_k8s_audit_log`):
- Category 1: `broker.dispatch()` returned a permitted `Effect`, `execution_class="k8s_pod"`,
  `K8sPodBackend.run()` invoked exactly once, `phase="Succeeded"`, `exit_code=0`.
- Category 2: `k8s_audit.parse_lines()`, reading the audit log independently, found exactly one
  event with `args=("create", "pods:default/<the same pod_name Siphonophore reported>")` and
  `success=True`. Empirically, the audit record's `user.username` for this event was
  `kubernetes-admin` (kind's default local admin identity for the `kubectl` calls
  `K8sPodBackend` shells out to) — confirmed by direct inspection during setup, not assumed.
- These two lines of evidence are about **different facts**: category 1 says the workload's
  container actually ran and exited 0 (kubelet-reported); category 2 says the API server accepted
  a `pods create` request for the same object name. Both true here; neither substitutes for the
  other.

**Direct-dispatch DENY** (`test_deny_direct_dispatch_internal_and_external_absence`):
- Category 1: `K8sPodBackend.run()` invoked zero times.
- Category 3: a live query for the precomputed label (`label_value_for(intent_id)`, known before
  dispatch since the caller supplies `intent_id`) returned zero Pods — a **current-state**
  absence claim, strengthened to "never created during this test's own run" only by combining it
  with category 1's direct non-invocation evidence, not asserted as an eternal claim on its own.
- Category 2: zero `pods create` audit events matching Siphonophore's own `sipho-` naming
  convention in the `default` namespace during the attempt window (attempt + 3s settle buffer) —
  **windowed and namespace-scoped, not name/label-specific to one execution**, because
  `k8s_audit.py` never reads labels and a denied dispatch never reaches `pod_name_for()` in the
  first place. This holds regardless of whether the id was known in advance — the audit channel's
  absence claim is bounded by time+namespace(+Siphonophore's naming convention) for a structural
  reason unrelated to precomputability. The `sipho-`-prefix filter was added after adversarial
  review found that namespace scoping alone does not exclude arbitrary non-Siphonophore activity in
  `default` — see "Adversarial review findings" below.

**CognitiveLoop DENY** (`test_deny_via_cognitive_loop_windowed_absence_only`):
- Category 1: `K8sPodBackend.run()` invoked zero times.
- Category 3: **no label-specific check is possible** — `parse_intent()` mints `intent_id`
  internally and it is lost when `GateViolation` propagates before an `Effect` exists. The check
  actually run is a global before/after count of Siphonophore-managed Pods in the namespace —
  strictly weaker than the direct-dispatch case's label-specific check.
- Category 2: same windowed/namespace-scoped shape as the direct-dispatch case (this channel was
  already windowed-only in both cases — the asymmetry against direct-dispatch is specific to
  category 3, not category 2).
- The honest, exact claim this test supports: *"during this test's own attempt window, no
  qualifying `pods create` API activity was observed in the `default` namespace, and no new
  Siphonophore-managed Pod appeared."* Not: *"this specific denied intent never ran"* — there is no
  surviving identifier to make that narrower claim about.

## What Stage 1 does NOT establish

- **No kernel-level confirmation of anything.** No eBPF, no process/syscall observation. The
  strongest positive confirmation of "a real container process ran" in this experiment is
  Siphonophore's own live-state poll (category 1/3-adjacent, self-reported) — a genuinely
  independent, kernel-level confirmation is exactly what eBPF (Stage 2, not attempted) would add.
  (A separate, later probe — not part of Stage 1's own test suite — has since obtained exactly that
  kernel-level confirmation for a standalone known process; see "Stage 2 precursor" below. It does
  not modify or extend Stage 1's own results above, which are unchanged.)
- **No proof that a K8s audit event implies process execution.** Explicitly, deliberately not
  claimed anywhere in this experiment's code or results.
- **No stronger DENY claim than what's stated above.** In particular, the CognitiveLoop DENY case's
  absence claim is a bounded, windowed statement — not a specific-workload non-execution proof.
- **No managed-cloud validation.** `kind` only.
- **No production-shaped correlation mechanism.** `correlate.py` is intentionally throwaway,
  experiment-scoped glue, not a reusable observer abstraction (see the work order's explicit
  prohibition on building one in this work unit).

## Adversarial review findings

A fresh reviewer read every file here plus the real AgentWatch/Siphonophore source, ran the real
test suite against the live cluster, and independently re-measured timing rather than trust
prose claims. Two real, fixable findings, both fixed before commit:

- **The `default`-namespace absence check's "excludes noise by construction" claim overclaimed.**
  Namespace scoping excludes kube-system bootstrap traffic, but not arbitrary non-Siphonophore
  activity in `default` — confirmed concretely: a manually-created `smoketest` pod from this
  cluster's initial smoke test was still sitting in the audit log's `default`-namespace history.
  Fixed by additionally requiring the audited object's name to carry Siphonophore's own `sipho-`
  prefix (`pod_name_for()`'s output charset is `[a-z0-9-]` only — confirmed by reading it directly,
  no path exists for an unprefixed name to reach this shape) — available without needing labels,
  since the object name is already embedded in the audit event's `resource_id`. This does not make
  the check name-specific to one execution; it only excludes non-Siphonophore activity, so the
  windowed/non-object-specific character of the DENY audit checks is unchanged.
- **`ABSENCE_SETTLE_SECONDS = 3.0` had no measured justification in the repository.** Fixed by
  directly measuring audit-log write latency against this same cluster: 5 runs of `kubectl create`
  → visible in the tailed audit log, worst case 0.28s, typical ~0.05s. 3.0s is roughly 10x the
  observed worst case, not an arbitrary round number — now stated as such in the code comment.

Twelve of the fourteen review items found no problem: the audit-side ALLOW evidence is genuinely
independent (never reads `effect`); no tautological same-source comparisons exist; no Warrant/
AgentWatch demo semantics leak in (grepped for `subject_id`/`GrantEvent`/`Warrant`/
`IdentityCorrelator`/`k8s_scope` — zero real hits); the audit channel is never described as proof of
process execution; `git diff` confirms zero lines changed under `siphonophore_core`/
`siphonophore_harness`; this directory is not imported by any production code or the `tests/`
regression suite; and the ALLOW/DENY and direct-dispatch/CognitiveLoop-DENY asymmetries are stated
explicitly rather than smoothed over.

## Non-blocking follow-up preserved from the prior phase

`decision.intent_id` is still reused as every backend's concrete execution-correlation identity by
convention (including `K8sPodBackend` here) — this experiment did not change or redesign that. See
the project memory / `/docket` entry: whether request/intent identity, attempted-execution identity,
and realized concrete-execution identity are actually distinct concepts remains an open question the
Kubernetes work is expected to inform, not one this work unit resolved.

## Stage 2 precursor: host-level eBPF observation-topology probe (2026-09-04)

**Not Stage 1. Not Stage 2 either — a narrow, prerequisite feasibility probe for Stage 2, run and
independently analyzed before any Stage 2 implementation.** The question this probe answers: can
agent-vm's **host** kernel observe a known process executing inside a vanilla `kind` Kubernetes
workload using AgentWatch's existing eBPF machinery, with enough cgroup/runtime evidence to
defensibly correlate that observation to the concrete Pod/container — i.e., does Stage 2 need an
in-cluster eBPF DaemonSet, or is host-level observation empirically sufficient on this environment?

Two separate identities were involved by design, so the interpretation could not simply agree with
itself: **maude** (privileged) executed the probe against a real `kind` cluster and produced a raw
evidence archive; **sipho-agent** (restricted, no elevated privilege) independently analyzed that
archive read-only, against the pinned AgentWatch source, without rerunning the privileged
experiment and without trusting maude's own interpretation of the result.

- Evidence archive: `/tmp/sipho-topology-probe-evidence.tar.gz`,
  SHA-256 `78e5687ecd0466c8b436162ea7e122bc21400bfe3ce168f995c6926ae7e35827` (verified independently by
  sipho-agent; exact match).
- AgentWatch checkpoint: `92037e9ee926ce817829d34923b914b93c16f152` — the same commit already
  recorded above under "AgentWatch checkout and revision" for Stage 1; the archived bpftrace program
  was confirmed byte-for-byte identical to this checkout's live `agentwatch.groundtruth.ebpf.BPFTRACE_PROGRAM`
  (direct string comparison, not assumed).
- Siphonophore checkpoint: this branch, at the commit immediately prior to this one (`edd59da`).

**Result: FULL SUCCESS.** All four pre-registered success criteria (defined before the evidence was
seen) passed:

1. AgentWatch's own unmodified `parse_lines()`, run against the raw bpftrace output, found exactly
   one EXEC event for the known marker (`sp-mk-725df4ab`), with the marker appearing directly in
   `exe` (`/tmp/sp-mk-725df4ab`) — pid `87776`, uid `0`, cgroup `189979`.
2. That event's cgroup identifier resolved, through AgentWatch's own unmodified
   `demo/k8s/ebpf/pod_lookup.py:pod_uid_from_cgroup_path()` applied to the host cgroup path, to Pod
   UID `b6a16766-05ee-446a-8ee5-ff96a817f47c` — matching the raw Kubernetes Pod object's
   `metadata.uid` exactly (Pod `sipho-topo-probe`, container ID
   `containerd://679f3691f7f1b20198548be43b86f265f9fff9be0ac4c973638f5ee89decbca9`).
3. An independently collected cgroup inode (`/proc/87776/cgroup` → `stat` of the corresponding
   `/sys/fs/cgroup` path, collected via a route that does not go through AgentWatch's own inode-walk)
   agreed exactly with the eBPF event's cgroup identifier: `189979 == 189979`.
4. Exactly one Kubernetes Pod/container was consistent with the complete evidence — confirmed
   against `crictl ps`/`crictl pods` (nine other, unrelated containers on the node, none sharing this
   container ID or Pod UID) and independently corroborated by a second, cgroup-free route: host `ps`
   ancestry from PID `87776` through the `containerd-shim-runc-v2 -id <sandbox>` parent to the same
   `crictl pods` sandbox ID.

The correlation does not depend on timing coincidence: the converging identifiers are a 128-bit Pod
UID and a 64-hex-character container ID, embedded verbatim in independently-collected artifacts
(cgroup path, raw `pod.json`, `crictl` output), not merely co-occurring timestamps.

**Narrow empirical claim (do not generalize beyond this):** on agent-vm's tested
`kind`/cgroup-v2/containerd/systemd-cgroup-driver topology, AgentWatch's existing host-level eBPF
observation can observe a process executing inside a Kubernetes workload and correlate the observed
cgroup to the concrete Pod/container identity, without requiring an in-cluster observer. This does
**not** establish: managed-Kubernetes behavior; other cgroup drivers/layouts or container runtimes;
adversarial-workload robustness; production reliability; that Siphonophore caused the observed
execution; Siphonophore's authorization correctness; or any DENY/non-execution evidence (this probe
only tested a permitted execution path, mirroring Stage 1's own ALLOW/DENY evidentiary discipline
above).

**Architectural consequence (empirical, not a design decision):** for the *minimum* Stage 2
experiment on agent-vm, an AgentWatch eBPF DaemonSet is not required — host-level observation is
empirically sufficient on this tested topology. A future bounded measurement helper would minimally
need `CAP_BPF`/root to load the probe (via the same caller-supplied-elevation shape
`agentwatch.groundtruth.ebpf_capture.py` already documents) plus host read access to
`/sys/fs/cgroup` and `/proc/<pid>/cgroup` — no in-cluster component, ServiceAccount token, or
`kubernetes.default.svc` reachability. **No such helper was designed or built as part of this
checkpoint;** this is a recorded consequence of the evidence, to inform — not preempt — Stage 2's
actual design.

**AgentWatch robustness finding (not fixed, by instruction; recorded as a caveat):**
`pod_lookup.py`'s `pod_uid_from_cgroup_path()` docstring describes the expected cgroup-path shape as
containerd's systemd driver embedding `kubepods-<qos>-pod<uid>.slice` directly. The actual path
observed on this `kind` node was `kubelet-kubepods-besteffort-pod<uid>.slice` — an extra `kubelet-`
prefix segment the docstring does not describe. Resolution still succeeded only because the
matching regex (`_POD_UID_RE`) is an unanchored `re.search`, which finds `kubepods-besteffort-pod...`
as a contiguous substring inside the longer real string. This worked here but is fortuitous relative
to what the code's own documentation claims; a differently-shaped cgroup naming scheme could
silently return `None` rather than erroring. Left unfixed per this checkpoint's scope (documentation
only, no AgentWatch changes).

**Failed attempts, preserved (not smoothed over):**
- *Attempt 1* (`exec /usr/local/bin/<marker> 300`) — fixture failure before any exec: `/usr/local/bin`
  does not exist in the `busybox:1.36` image used. Independently confirmed: zero marker-related EXEC
  events anywhere in this attempt's raw bpftrace output.
- *Attempt 2* (`exec /tmp/<marker> 300`) — produced a **genuine kernel-level EXEC** of the marker
  (pid `86889`, cgroup `189514`, same marker string in `exe`), immediately followed by container exit
  127: BusyBox's multi-call binary dispatches its applet by `argv[0]`, and a bare `exec` sets
  `argv[0]` to the exec'd path itself, which BusyBox did not recognize as a known applet. The raw
  kernel EXEC evidence for this attempt was independently re-verified from the raw bpftrace output,
  not merely accepted from the archive's own manifest.
- *Attempt 3* (`exec -a sleep /tmp/<marker> 300`) — corrected `argv[0]` to a recognized BusyBox
  applet (`sleep`); supplied the complete, successful correlation evidence summarized above.

**Operational note (not a scientific topology claim):** this checkpoint's own independent analysis —
archive integrity verification, AgentWatch source inspection, unmodified-parser execution, and the
full correlation/falsification pass above — was run entirely by a hands-free `sipho-agent` session
with its own tool-permission prompts bypassed, constrained only by the Linux/VM account boundary
(no sudo, no elevation available or used), with no intermediate user approvals requested and no
repository or system changes made. This is recorded as one data point that the "hands-free agent +
externally constrained OS identity" pattern worked for this specific read-only analysis workload — not
as a general claim that this pattern is sufficient or safe for arbitrary autonomous workloads.
