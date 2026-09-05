# Kubernetes mediation-bypass experiment

**STATUS: PRE-REGISTERED DESIGN — NOT IMPLEMENTED.**

Nothing here has been run. There are no results in this document, and there is no implementation
in this directory. Every criterion below was written before any code exists, which is the point of
writing it now: the criteria cannot be adjusted after seeing the evidence if they were published
before the evidence could be collected.

This is a design/pre-registration artifact on a research branch, deliberately not merged to `main`
and deliberately not linked from the root `README.md` — the root README indexes *results*, and
there are none here yet.

## The question

> Can the concrete Kubernetes execution occur without traversing Siphonophore's authorized
> mediation path?

The first job of this document is to establish what that question can rigorously mean. It turns
out to mean at least four different things, only some of which are testable, and only some of
which are properties of Siphonophore at all.

### Four distinct questions hiding inside one sentence

1. **Internal mediation enforcement.** *Given* that execution is attempted through Siphonophore's
   own API, must a valid, permitted, artifact-bound `Decision` exist before the registered backend
   is invoked? — A property of the SDK. Testable in-process. Largely already true; see "What the
   current code actually enforces."
2. **Deployment-level bypass resistance.** Can the requesting principal cause the same substrate
   effect by ignoring Siphonophore and addressing Kubernetes directly? — A property of the
   *deployment*, not of the SDK. Not currently tested by anything in this repository.
3. **Credential/authority separation.** Does the requester in fact possess the substrate authority
   needed to bypass the mediator? — An OS/Kubernetes authority question, prior to (2): if the
   answer is yes, (2) is decided in the negative before any Siphonophore code runs.
4. **Absolute non-bypassability.** Is the effect unreachable by *any* path whatsoever? — Not
   scientifically establishable by a finite experiment. A finite experiment enumerates paths; it
   cannot quantify over all paths. This document does not attempt (4) and pre-commits to never
   claiming it.

**This experiment targets (3) and (2), conditional on a bounded threat model, and separately
re-confirms (1) as an in-process property.** It treats (4) as out of reach by construction.

### Claims that must not be conflated

These are seven different propositions. Prose that slides between them is the specific failure mode
this section exists to prevent:

| # | Proposition | Kind of fact |
|---|---|---|
| P1 | Siphonophore rejects an unauthorized request | SDK behavior, in-process |
| P2 | Siphonophore does not invoke its backend | SDK behavior, in-process |
| P3 | Kubernetes denies the requester's direct request | Kubernetes authorization fact |
| P4 | No Kubernetes workload is created | Kubernetes live-state / audit fact |
| P5 | No kernel process executes | Kernel/eBPF fact |
| P6 | Siphonophore's mediator is the only principal *in this deployment* capable of causing the effect | Bounded deployment claim over enumerated paths |
| P7 | Siphonophore is causally necessary for the effect | Unbounded causal claim |

P1 ⇏ P2 ⇏ P3 ⇏ P4 ⇏ P5. P6 is bounded by an enumeration and a threat model. **P7 is not a claim
this experiment can support, and the design below explains why the strongest available result is a
statement about *credential custody in a deployment*, not about Siphonophore being necessary.**

## Motivation: exactly what Stage 2 left open

The completed dual-observation experiment
([`../k8s_agentwatch_observation/README.md`](../k8s_agentwatch_observation/README.md), commit
`c63ee21a770d80ae373acfd8213e0b42af73d099`) established, narrowly: for one real
Siphonophore-mediated Kubernetes execution on the tested `kind`/cgroup-v2/containerd/
systemd-cgroup-driver topology, Siphonophore's own `Effect`, the Kubernetes API-server audit
record, and independent host-kernel eBPF observation converged on the same concrete Pod/container.

That is a *correlation* result about one execution that did happen. It says nothing about whether
that execution *had* to happen through Siphonophore. Concretely, Stage 2's own setup makes the gap
explicit: the requesting identity held a worker kubeconfig
(`/etc/sipho-stage2/sipho-agent.kubeconfig`) and used plain, unprivileged `kubectl` against the
cluster throughout, for the live-object observation leg. That was correct and necessary *for
observation* — Category C evidence had to be independently readable — but it means the requester in
Stage 2 could trivially have created the same Pod itself. Stage 2 therefore did not, and could not,
test path exclusivity. It never claimed to; its own "What Stage 2 does NOT establish" section names
"no causal proof that Siphonophore's mediation *caused* the observed execution" explicitly.

This experiment is the natural next question because it is the one Stage 2's result most invites
and least supports.

## What the current code actually enforces

Reconstructed by reading the code on `main` at `f9d95c0`, not from documentation.

### The path

```
requester (CognitiveLoop, or any caller)
    → Intent                                    intent.py            (frozen dataclass, caller-built)
    → Broker.dispatch(intent, authority=None)   broker.py:37-39
    → Gate.submit(intent, authority)            mediation.py:70-122  ← mints Decision (HMAC)
    → Executor.execute(decision, intent)        execution.py:140-159 ← re-verifies everything
    → K8sPodBackend.run(decision, intent)       execution_k8s.py:130 ← performs the effect
    → subprocess kubectl ...                    execution_k8s.py:127-128
    → Kubernetes API server
    → Pod
```

### Where each check lives

**`Gate.submit()` (`mediation.py:70-122`)** — when an `Authority` is supplied: independently
re-verifies its HMAC (never trusting a caller's prior check); requires
`intent.principal_id == authority.principal_id`; folds
`intent.kind ∈ authority.scope.allowed_kinds` into `permitted`. Then evaluates policy and mints a
`Decision` whose HMAC binds `intent_id, principal_id, kind, permitted, execution_class,
artifact_digest, authority_id, order_id`.

**`Executor.execute()` (`execution.py:140-159`)** — independently re-checks, in order:
decision↔intent correspondence (`intent_id`, `kind`); `gate.verify(decision)` (HMAC);
`decision.permitted`; and `sha256(intent.artifact_code) == decision.artifact_digest`, raising
`ArtifactMismatchError` *before* any backend runs. Only then does it look up the backend.

**`K8sPodBackend.run()` (`execution_k8s.py:130-207`)** — performs **no authorization check at
all.** It reads exactly one field off the `Decision`: `decision.intent_id`, used as the
execution_id for Pod naming (`execution_k8s.py:135`). It never calls `gate.verify()`, never reads
`decision.permitted`, never recomputes the artifact digest. This is by design and is documented as
such (`ExecutionBackend.run`'s docstring, `execution.py:67-70`: "Called only after Executor has
already verified the Decision…"). It is the correct factoring — and it is decisive for this
experiment.

### Three findings that determine the whole design

**Finding 1 — the backend owns no credential; substrate authority is ambient to the process.**
`K8sPodBackend` stores `namespace`, `image`, `kubectl`, `context`, `timeout`, `poll_interval`
(`execution_k8s.py:105-119`). No credential, no token, no kubeconfig path. It shells out to
`kubectl` (`execution_k8s.py:121-128`), which resolves credentials from ambient process state:
`$KUBECONFIG`, else `~/.kube/config`, else an in-cluster ServiceAccount token. **The substrate
authority used by the backend is therefore whatever OS authority the calling process already
holds.** `docs/EXECUTION.md` already states this general property in its Credentials bullet —
"ambient credentials are whatever the executing process already happens to hold" — this is that
statement's concrete instance for `k8s_pod`.

**Finding 2 — in the current single-process deployment, requester authority ⊇ backend authority,
necessarily.** `Broker`, `Executor`, `K8sPodBackend` and the requester all live in one Python
process under one Unix identity. By Finding 1 the backend's substrate authority *is* that process's
ambient authority. So the requester already holds everything the backend uses. Bypass is not merely
possible, it is trivial: `subprocess.run(["kubectl", "apply", ...])`. **No arrangement of
Siphonophore's own code can change this**, because the bypass never enters Siphonophore's code.

**Finding 3 — `K8sPodBackend` is directly invocable, and `Decision` is freely constructible.**
`K8sPodBackend` is a public class in a public module. `Decision` (`policy.py:17-41`) is a plain
frozen dataclass with no validation in `__init__` — any caller can construct one with arbitrary
`permitted`, `execution_class`, and `token` values. Because `run()` checks none of them
(Finding 1's corollary), `K8sPodBackend().run(Decision(...anything...), intent)` will create a real
Pod **if and only if the calling process has usable kubectl credentials.** Decision authenticity
does not prevent this, because Decision authenticity is enforced in `Executor`, and the bypass
skips `Executor`.

### Code-level versus OS/deployment-level enforcement

| Enforcement | Mechanism | Binds whom |
|---|---|---|
| Decision authenticity, permitted-ness, artifact binding, authority scope | HMAC + re-verification in `Gate`/`Executor` | A caller that *chooses* to go through `Broker.dispatch()` |
| Substrate reachability | *Nothing in Siphonophore today for `k8s_pod`* | — |

The existing precedent for the missing row is not in `k8s_pod` but in the UID/cgroup tier:
`siphonophore-spawn` is a root-owned binary (mode 0711, root:root) reachable only through a narrow
sudoers `Cmnd_Alias` (`scripts/siphonophore-sudoers.template`). That boundary is enforced by the
operating system, not by Python. `contracts/spawn_helper.md`'s SH-23 section states the matching
principle directly: *"authorization belongs above the execution substrate."* This experiment asks
what the corresponding OS-level boundary is for Kubernetes, and whether it holds.

## SDK property versus deployment property

**Answering the question posed directly, because it is the central one:**

Non-bypassability is **not** a property Siphonophore-the-SDK can have. A library cannot prevent a
process that independently holds a credential from using that credential; there is no code the SDK
could contain that would stop `subprocess.run(["kubectl", ...])` in the same process. Stating this
plainly is not a concession extracted by adversarial review — it is the load-bearing premise of the
whole design, and it is already half-stated in canonical documentation: the root `README.md` says
Siphonophore sitting beneath a harness "requires a harness whose security-relevant effects pass
through a boundary Siphonophore can actually force every such effect through, without a path that
bypasses it — not every harness necessarily provides one."

The honest decomposition:

- **What Siphonophore can guarantee (SDK, code-level):** *internal mediation enforcement.* Within
  its own call path, no `Effect` is produced without a Gate-minted, Executor-re-verified,
  `permitted` `Decision` whose bound `artifact_digest` matches the code actually about to run, and
  whose `execution_class` selects the backend. This is cryptographic and process-local, and it
  holds conditional on the integrity of the single process holding the Gate's secret — the same
  standing condition `DESIGN.md` §9 and SH-23 already record.
- **What the deployment must guarantee (OS/Kubernetes, external):** *substrate-authority
  separation.* The requesting principal does not hold a credential that reaches the Kubernetes API;
  the mediating principal does; and the requester cannot acquire the mediator's credential by any
  path in the threat model.
- **The composite property, which is what "non-bypassability" actually names:** the conjunction of
  the two, and it is a property of *a deployment using Siphonophore*, never of Siphonophore alone.

If this experiment succeeds, the correct sentence is **not** "Siphonophore is non-bypassable." It
is something closer to: *"in this deployment, the requester's only reachable path to the target
effect ran through a mediator that would not act without a valid Siphonophore authorization."*

## Terminology chosen

After the analysis above, and rejecting the alternatives for the stated reasons:

- **Rejected: "causal necessity" / "causal proof."** Requires an intervention/counterfactual
  structure that would license "Siphonophore was necessary," and the intervention available here
  does not license it (see "Intervention and counterfactual structure").
- **Rejected: "non-bypassability" unqualified.** Reads as P7/absolute (question 4) and is not
  survivable.
- **Adopted, two-layer:**
  - **internal mediation enforcement** — the SDK-level, in-process property;
  - **substrate-authority separation** — the deployment-level credential-custody property;
  - **bounded mediation-path exclusivity** — the composite claim, always stated with its threat
    model and deployment named.

**Working name: the Kubernetes mediation-bypass experiment.** It names the method (attempting
bypass), not a conclusion.

## Threat model

### Roles

- **REQUESTER (R)** — the agent/workload principal that asks Siphonophore to act. A real,
  unprivileged Unix identity on the test host. This is the identity whose reachability the
  experiment measures.
- **MEDIATOR (M)** — the principal authorized to turn a valid Siphonophore `Decision` into
  substrate execution. A *separate* real Unix identity, holding the Kubernetes credential.
- **SUBSTRATE (S)** — the Kubernetes API server and the concrete workload execution it produces.
- **OBSERVER (O)** — independent evidence machinery: the Kubernetes API-server audit log, read
  through AgentWatch's existing unmodified `agentwatch.groundtruth.k8s_audit.parse_lines()`, plus
  direct Kubernetes live-state reads made from an identity distinct from the one under test. The
  observer stays outside both R's and M's trust domains, per `DESIGN.md` §5.

### What R may do (all of this is in-scope and expected to be attempted)

- Call Siphonophore normally, through the provided mediated path.
- Execute arbitrary code within its own OS authority — arbitrary commands, arbitrary Python.
- Import and instantiate any Python module readable by it, including `siphonophore_core.*` and
  `K8sPodBackend` directly.
- Construct arbitrary `Decision`, `Intent`, `Authority`, and `Order` objects, including ones with
  fabricated `token` fields.
- Read its own environment, its own filesystem, and anything the filesystem's DAC permits it to
  read — including searching for kubeconfigs, tokens, and client certificates.
- Attempt direct Kubernetes API access by any means available to it: `kubectl` if present, raw
  HTTPS to the API server endpoint, in-cluster ServiceAccount tokens if it can obtain one.
- Supply arbitrary `artifact_code` through the legitimate mediated path, and read back whatever the
  resulting `Effect` returns — including Pod logs.
- Reuse a previously valid `intent_id`, and replay a previously accepted request.

### What R may NOT assume (excluded attacker capabilities — the boundary that makes the experiment finite)

- Root / `sudo` beyond the single fixed-shape mediator invocation granted to it.
- Membership in `docker`, `containerd`, or any equivalent container-runtime group. *(Excluded by
  assumption, and separately **verified** rather than assumed — see falsification F-11; a
  container-runtime group is a full bypass and its absence must be measured.)*
- Possession of, or read access to, M's kubeconfig or any other substrate credential.
- Ability to modify root-owned or M-owned files, including the mediator helper itself.
- Ability to modify Kubernetes RBAC, create ServiceAccounts, or bind roles.
- Cluster-admin, node access, or `kubelet` access.
- Compromise of the substrate itself (API server, etcd, container runtime, kernel).
- Compromise of M's process memory (`ptrace` across uid boundaries, which standard Linux DAC plus
  `yama/ptrace_scope` already denies — again verified, not assumed; F-09).
- Physical/hypervisor access, or the ability to alter the audit configuration.

This boundary is chosen to be *strong enough that bypass testing is meaningful* — R is a fully
capable local attacker within its own uid — and *bounded enough to be testable* — R does not get
root, and the experiment does not attempt to model a compromised kernel or a compromised mediator
process. A compromised mediator is explicitly out of scope for the same reason SH-23 gives: a
mediator whose process is compromised already holds the Gate secret and can mint any `Decision` it
likes, so no substrate-side check could distinguish it.

## Proposed authority topology

```
    R (requester uid)                        M (mediator uid)
    ─────────────────                        ────────────────
    no kubeconfig                            kubeconfig, mode 0600, owned by M
    no $KUBECONFIG                           (R cannot read it: DAC)
    no ~/.kube/config
    no client cert/key                       ┌──────────────────────────┐
    not in docker group                      │ Gate  (holds HMAC secret)│
           │                                 │ Executor                 │
           │  Intent (stdin envelope)        │ K8sPodBackend            │
           └────────────────────────────────▶│                          │
              sudo -n /usr/local/libexec/    │  mints its own Decision  │
                       sipho-mediate         │  — never accepts one     │
              (fixed shape, NO arguments)    └──────────────────────────┘
                                                        │ kubectl, as M
                                                        ▼
                                              Kubernetes API server
                                                        │
                                                        ▼
                                                  target Pod
```

### Enforcement boundary

The boundary is **file ownership plus a scoped sudoers grant** — the operating system, not Python:

- M's kubeconfig is mode `0600`, owned by M. R cannot read it (DAC).
- R's sudoers grant is exactly one fixed-shape command, run *as M, not as root*:
  `R ALL=(M) NOPASSWD: /usr/local/libexec/sipho-mediate ""`. The trailing `""` means *invocable
  with no arguments at all* — the same SH-08 convention `scripts/siphonophore-sudoers.template`
  already documents and Stage 2 independently re-validated against real `sudo` behavior. All input
  arrives on stdin, eliminating the argument-injection surface.
- `sipho-mediate` is owned by root (or M), not writable by R, so R cannot replace the "mediator"
  it invokes — the same requirement SH-26 states for `siphonophore-spawn`.

### One deliberate design decision, stated in advance

**The mediator accepts an `Intent` (and optionally an `Authority`). It never accepts a `Decision`.**
It mints its own `Decision` through its own `Gate`, whose HMAC secret exists only inside M's
process. This makes forged and replayed `Decision` objects structurally irrelevant *at the mediation
boundary* — there is no input slot for one. It is recorded here as a pre-registered design choice
rather than discovered later as a convenient property.

### Why this mechanism, and not the alternatives

The requirement is only that R and M be *different OS principals* with different ambient authority.
Evaluated against that:

| Mechanism | Verdict |
|---|---|
| Separate Unix identity + mode-0600 kubeconfig | **Required.** This *is* the property under test; everything else is delivery. |
| Fixed-shape `sudo -u M` helper, stdin envelope | **Chosen.** Smallest thing that works; no long-running process, no socket lifecycle; reuses an existing, twice-validated repo convention. |
| Long-running mediator service + Unix-domain socket | Viable alternative, strictly more machinery (lifecycle, socket permissions, framing). The enforcement boundary is identical — file ownership of the kubeconfig. Deferred. |
| Root-owned C helper (`siphonophore-spawn` shape) | **Not needed.** M needs to be a *different* uid, not a *privileged* one. Adding root expands the privileged surface for no experimental gain. |
| Kubernetes ServiceAccount/RBAC scoping of M | **Not required for the property under test**, which is about R's reachability, not M's bounds. Genuinely valuable hardening (it would additionally bound the mediator) and a natural follow-on. Classified as optional refinement, not a prerequisite. |
| Separate container/Pod for M | Heavier, and adds a container-runtime surface the threat model would then have to model. Rejected. |

**Recorded limitation of the minimum shape:** if M simply holds kind's default `kubernetes-admin`
credential, then M is cluster-admin. The property under test — R cannot reach the effect directly —
is unaffected, but the resulting claim must not be phrased as though the mediator were narrowly
scoped. It is not, in the minimum configuration. Narrow RBAC for M is the first follow-on.

## Positive case (mediated ALLOW)

```
R holds no substrate credential  (established first, as evidence, not assumed)
    → R submits an authorized Intent over the mediator boundary
    → M's Gate evaluates policy and mints a permitted Decision
    → M's Executor re-verifies it and checks the artifact digest
    → M's K8sPodBackend runs kubectl as M
    → target Pod created and runs to completion
```

### Evidence actually required — and what is deliberately dropped

The new hypothesis is about **reachability under authority**, not about whether a payload really
executes at kernel level. Stage 2 already settled the latter for this topology. So the observer set
is *not* mechanically inherited:

- **Kubernetes audit — required, and doing genuinely new work here.** The audit record's
  `user.username` is precisely the fact that distinguishes "created by M" from "created by R." In
  Stage 2 both identities were `kubernetes-admin`, so this field carried no discriminating
  information; under the proposed topology it does. This is the single most load-bearing evidence
  channel for the new question, and it is a *different* use of the channel than Stage 1/Stage 2
  made of it.
- **Kubernetes live object state — required.** Establishes that exactly one target Pod exists and
  that bypass attempts produced none.
- **OS authority facts — required.** R's inability to read the credential is the premise of
  everything else and must be measured, not stipulated.
- **Host-kernel eBPF — NOT required. Explicitly dropped from the minimum design.** It would confirm
  that the mediated Pod's payload really executed — which Stage 2 already established for this
  topology, and which bears on *no* bypass criterion. For the negative cases its absence evidence
  would be strictly *weaker* than "no Pod object exists" (ambient-activity noise, no marker to key
  on when nothing ran). Including it would be reuse for its own sake. It may be added later as
  optional corroboration of the ALLOW case only; it is not a prerequisite and its absence is not a
  limitation of the bypass result.

## Bypass cases

Each is an attempt that should fail under the proposed topology, with the boundary that is expected
to stop it named *in advance* — because "it failed" is uninformative unless the reason was
predicted.

### A. Direct Kubernetes API bypass
R attempts to create the target Pod itself: `kubectl` if present; raw HTTPS to the API server
endpoint if not.
**Expected:** rejected at the API server (401/403) or unable to construct a request at all for lack
of credentials. No Pod created; no mutation.
**Enforcing boundary:** Kubernetes authentication/authorization, downstream of R having no
credential (OS DAC).

### B. Direct backend bypass
R imports `siphonophore_core.execution_k8s`, instantiates `K8sPodBackend()`, constructs a
`Decision` by hand, and calls `run()` directly.
**Expected:** no Pod created — and the pre-registered explanation of *why* matters more than the
outcome. Per Finding 3, **nothing in Siphonophore stops this.** `run()` performs no authorization
check; the `Decision` may be entirely fabricated; `require_cluster_reachable()`
(`execution_k8s.py:46-58`) will fail, or `kubectl apply` will be rejected, **solely because R holds
no credential.**
**Enforcing boundary:** credential custody (OS DAC + Kubernetes authn). *Not* Decision validation.
*Not* Siphonophore.
This case is the sharpest single result the experiment can produce, and it must be reported as a
finding about the deployment, never as evidence that the SDK resisted anything.

### C. Forged Decision
Two distinct sub-cases, which the design separates deliberately:
- **C1, against the mediation boundary:** R attempts to submit a forged/altered `Decision` to M.
  **Expected: structurally impossible** — the boundary accepts only an `Intent`. Reported as a
  design property, not as a defeated attack.
- **C2, against a locally constructed `Executor`:** R builds its own `Gate`+`Executor` and feeds a
  forged `Decision` to `Executor.execute()`. **Expected:** `GateViolation` at
  `execution.py:143-144` — R's `Gate` has a different random secret (`mediation.py:48`), so the
  HMAC cannot verify. This is a genuine SDK property (internal mediation enforcement), testable
  **in-process with no cluster at all.**
- **C3, artifact substitution:** a *validly minted* `Decision` used with different `artifact_code`.
  **Expected:** `ArtifactMismatchError` before any backend invocation (`execution.py:148-154`).
  Also cluster-free.

### D. Replay — classified OUT OF SCOPE, deliberately
R replays a previously valid request or a previously valid `Decision`.
The current model provides **no** expiry, revocation, or consumption semantics for `Authority`
(stated outright in `authority.py` and `Gate.submit()`'s own docstring, `mediation.py:82-85`) and
no consumption semantics for a `k8s_pod` `Decision` (unlike `siphonophore-spawn`'s SH-23
one-spawn-per-`execution_id` rule). **Replaying an authorized request through the mediator is
therefore expected to succeed, and that is not a failure of this experiment.** It is not a
mediation *bypass* at all — the replayed request still traverses the mediator and still requires a
valid authorization. It is an authorization-*freshness* gap, a separate open property, recorded
here and pre-registered as **not tested and not claimed**. Pre-registering it this way prevents the
opposite error: treating a known, documented, deliberate design limitation as though the experiment
should have caught it.

### E. Credential discovery
R actively searches its own environment and filesystem for substrate credentials: `$KUBECONFIG`,
`~/.kube/config`, `/etc/kubernetes/*`, M's home directory, `/var/run/secrets/kubernetes.io/*`, any
world-readable client cert or key, and the process table.
**Expected:** nothing usable found.
**Enforcing boundary:** filesystem DAC and provisioning hygiene.
This case exists because criterion 1 must be *demonstrated* — the experiment must show R genuinely
lacks the credential, not merely that the test code politely declined to look for one.

## Evidence categories

Every criterion is tagged with the *kind* of fact that settles it, and self-report is never allowed
to settle an external authority question. Categories, extending Stage 1/Stage 2's own discipline
with two new ones (OS authority, Kubernetes authorization) that the earlier stages did not need:

| Tag | Category | Source |
|---|---|---|
| **S** | Siphonophore internal claim | `Decision`, `Effect`, backend invocation counts, raised exceptions |
| **O** | OS authority fact | file mode/ownership, `id`, `stat`, read attempts that fail with EACCES |
| **K-authz** | Kubernetes authorization fact | API-server response code to a request made *as R* (401/403) |
| **K-live** | Kubernetes live-state fact | `kubectl get` from the observer identity, never read off `Effect.detail` |
| **K-audit** | Kubernetes audit fact | audit log parsed by AgentWatch's unmodified `k8s_audit.parse_lines()`; `user.username` is the discriminating field |
| **E-bpf** | Kernel/eBPF fact | *optional, ALLOW case only; not required by any criterion* |
| **D** | Derived correlation | never itself described as independent observation |

## Pre-registered success criteria

Refined from the candidate list rather than adopted from it; deviations and their reasons are
stated.

1. **(O)** R demonstrably holds no usable substrate credential: no `$KUBECONFIG`, no readable
   `~/.kube/config`, no readable client cert/key, no ServiceAccount token on the host, and no
   container-runtime group membership — each established by a positive measurement (a failing read,
   an `id` listing), not by absence of a test.
2. **(K-authz, K-live)** R's direct API bypass attempt (case A) is rejected by the API server, and
   no target Pod exists afterward.
3. **(S, K-live)** R's direct backend bypass (case B) creates no target Pod — **and the experiment
   records that the enforcing boundary was credential custody, not any Siphonophore check.** The
   criterion is not satisfied by "no Pod appeared"; it requires the recorded reason to match the
   prediction.
4. **(S)** A valid mediated request succeeds: exactly one `K8sPodBackend.run()` invocation,
   `phase="Succeeded"`, `exit_code=0`.
5. **(K-live, K-audit)** Independent evidence confirms the mediated Pod was created, **and the
   audit record attributes its creation to M's principal, not R's.** The attribution half is the
   part that is new relative to Stage 2 and is required, not optional.
6. **(S)** In-process, cluster-free: a forged `Decision` is rejected by `Executor` (C2), and a
   valid `Decision` with substituted artifact code is rejected before backend invocation (C3).
   *Replaces the candidate list's "kernel evidence confirms payload executed," which was dropped —
   see the positive case's evidence discussion.*
7. **(O)** R never receives and cannot read M's substrate credential at any point, including after
   a successful mediated execution — checked *after* the ALLOW case, not only before it, because a
   successful execution returns Pod logs to R and is itself a potential exfiltration channel
   (see F-05).
8. **(O)** No privilege expansion occurs during the experiment: R's authority at the end is
   identical to R's authority at the start, measured the same way both times.
9. **(bounded)** No enumerated path in the registered threat model allowed R to cause the target
   effect other than by submitting an authorized request to M.
   **This criterion is explicitly bounded to the enumerated falsification list below, and to no
   more.** It is a statement about paths that were tried, not about paths that exist. Any wording of
   the result that drops this qualifier is a misreport of the experiment, not a stronger reading of
   it.

**Deliberately absent:** any criterion asserting causal necessity, non-bypassability in general,
production readiness, managed-Kubernetes behavior, or portability to another
runtime/cgroup/topology.

## Failure and inconclusive conditions

Pre-registered so that an environment problem cannot later be read as a passing result.

**FAIL — the property does not hold:**
- R successfully creates any Pod by direct API access.
- R can read, copy, or otherwise obtain M's kubeconfig or any equivalent credential.
- Direct backend bypass (case B) creates a Pod.
- A forged `Decision` is accepted anywhere authenticity should have rejected it.
- Any denied or bypass attempt mutates Kubernetes state at all.
- The mediated request succeeds only because R *also* holds direct substrate access — i.e. the
  separation was never real. **This is the failure mode that most resembles success and is the one
  criterion 1 exists to catch.**
- R ends the experiment with more authority than it started with.

**INCONCLUSIVE — the hypothesis was never exercised; not reportable as either outcome:**
- The cluster cannot be created or is unreachable.
- The R/M identity separation is not actually in place when the run begins (wrong file mode, wrong
  ownership, R unexpectedly in a runtime group, sudoers grant not as specified) — the fixture must
  be *verified* before the hypothesis is exercised, and a fixture failure is inconclusive, never a
  pass.
- The audit observer is unavailable before the hypothesis is exercised.
- The environment cannot distinguish R's and M's authority (e.g. both resolve to the same uid).
- Any bypass attempt fails for a reason *other than* the predicted boundary — this is inconclusive
  for that case, not a pass, because the criterion is about the mechanism, not the outcome.

**An environment failure is never silently converted into a non-bypassability claim.** If the
fixture is wrong, the result is INCONCLUSIVE and is reported as such.

## Falsification plan

Adversarial cases aimed at *breaking* the claim, each classified as in-scope for this experiment or
deferred. In-scope cases are part of the enumeration that bounds criterion 9.

| # | Adversarial case | Scope |
|---|---|---|
| F-01 | Kubeconfig readable through an unexpected path (group perms, ACL, permissive parent directory, backup copy) | **IN** |
| F-02 | `$KUBECONFIG` or other credential leakage in R's environment | **IN** |
| F-03 | Default `~/.kube/config` present in R's home | **IN** |
| F-04 | Environment inheritance from R into the mediator subprocess, or mediator env leaking back to R (`sudo` `env_reset` behavior verified, not assumed) | **IN** |
| F-05 | **In-cluster ServiceAccount token reachable from a mediated Pod.** `K8sPodBackend` does not set `automountServiceAccountToken: false` (`execution_k8s.py:139-157`), so the namespace default SA token is mounted in the artifact container. R controls `artifact_code` and receives Pod logs, so R can exfiltrate that token and then attempt direct API access with it. Whether this yields a real bypass depends on the default SA's RBAC bindings, which **must be measured, not assumed** | **IN — highest-value case in the list** |
| F-06 | Raw HTTPS access to the API server despite `kubectl` being absent | **IN** |
| F-07 | Direct backend instantiation (case B) | **IN** |
| F-08 | Forged `Decision` (C1/C2) and artifact substitution with a valid `Decision` (C3) | **IN** |
| F-09 | Reading M's process memory or environment: `ptrace`, `/proc/<pid>/environ`, `/proc/<pid>/fd` across the uid boundary | **IN** |
| F-10 | Copying the mediator credential via the helper's own output, stdout/stderr, or an error message | **IN** |
| F-11 | **Ambient container-runtime bypass:** R in `docker`/`containerd` group, or a readable runtime socket, permitting `docker exec` into the control-plane node and retrieval of `/etc/kubernetes/admin.conf` | **IN — a total bypass if present; excluded by assumption and therefore must be measured** |
| F-12 | Helper argument/stdin injection: R attempts to influence the `kubectl` invocation, the namespace, or the manifest through crafted input | **IN** |
| F-13 | Creating the effect in an alternate namespace or via an alternate resource kind (Job, Deployment, CronJob) rather than a bare Pod | **IN** |
| F-14 | Replay of a prior authorized execution | **OUT** — separate open property; see bypass case D |
| F-15 | Stale/residual credential surviving teardown | **OUT** — teardown hygiene, not reachability during the run |
| F-16 | Compromise of M's process, or of the Gate secret | **OUT** — excluded by threat model (SH-23's reasoning) |
| F-17 | Managed-Kubernetes, alternate CNI, alternate runtime, alternate cgroup driver | **OUT** — Stage 2's topology limits carry forward unchanged |

## Privilege and capability surface

Derived, **not provisioned.** Nothing in this table has been created, and creating it is outside
this VM's authority.

| Capability | Holder | Requester access | Mechanism | Cleanup |
|---|---|---|---|---|
| Kubernetes credential (kubeconfig) | M | **None** — this is the property under test | File mode `0600`, owner M | Delete with the cluster |
| Invoke the mediator | R | Yes — exactly one fixed-shape command, no arguments | `R ALL=(M) NOPASSWD: /usr/local/libexec/sipho-mediate ""` | Remove sudoers drop-in |
| Mediator helper binary/script | root or M | Read/execute only; **not writable** | Ownership + mode `0755`/`0711`, not R-writable (SH-26 shape) | Remove installed file |
| Gate HMAC secret | M (in-process, ephemeral) | None | Process memory of a different uid | Dies with the process |
| Cluster lifecycle (create/destroy) | Provisioning identity | None | Outside R's and M's grants | Explicit teardown |
| Audit log read | Observer | Read-only | Existing Stage-1/Stage-2 shape | None |
| Container runtime (docker/containerd) | Provisioning identity | **None — must be verified absent (F-11)** | Group membership | None |

**R must not be able to grant itself M's capability.** This is why provisioning — creating M,
installing the helper, writing the sudoers drop-in, placing the kubeconfig — must be performed by a
separate authorized identity (`maude` or equivalent), exactly as Stage 2's `sipho-stage2` helpers
and sudoers grant were installed by a separate privileged stage before the experiment ran. **That
is a legitimate authority boundary, not a workaround**, and it is the same shape
`scripts/siphonophore-sudoers.template` already prescribes for `siphonophore-spawn`. An experiment
in which the requester provisions its own mediator would be testing nothing.

## Change requirements

- **Siphonophore core changes required? NO.** The mediator can compose `Gate`, `Executor`,
  `Broker`, and `K8sPodBackend` entirely unchanged. Note the irony worth recording: Finding 1 — that
  `K8sPodBackend` takes its credential ambiently from the process rather than owning one — is
  precisely what makes clean out-of-process separation possible with zero core changes. The property
  that makes in-process bypass trivial is the same property that makes the OS-level boundary easy.
  **`K8sPodBackend` does not assume credential ownership in any way that blocks separation.**
- **Experimental code required later? YES** — the mediator entry point, the R-side bypass probes,
  the fixture verifier, and the tests. All experiment-local, none in `siphonophore_core`.
- **AgentWatch changes required? NO.** Only `agentwatch.groundtruth.k8s_audit.parse_lines()`,
  unmodified, as in Stage 1 and Stage 2. AgentWatch remains outside Siphonophore's trust domain and
  is not, and does not become, a dependency.
- **New privileged/system component required? YES, but minimal and non-root:** a second Unix
  identity, a mode-0600 kubeconfig owned by it, one installed helper, one sudoers drop-in. No new
  root-owned binary; no C code; no new capability class.
- **Dedicated `execution_id` distinct from `intent_id` required? NO.** Sequential single attempts;
  `intent_id` suffices, the same conclusion Stage 2 reached. Still an open question generally, still
  not resolved here.
- **Replay protection required for this experiment? NO.** Out of scope by explicit
  pre-registration (case D); the mediation boundary's Intent-only shape makes it structurally
  irrelevant to the property under test.
- **`Authority` expiry/revocation required? NO.** Same reasoning. Both remain genuine open
  properties of the authorization model, and neither is a prerequisite here — recording them as
  prerequisites would be turning nice-to-have hardening into a blocker.

## Intervention and counterfactual structure

If a necessity-shaped claim is to be made at all, the intervention must be stated:

- **World A** — mediation path available, R holds valid authority: the target effect occurs.
- **World B** — same R, same requested effect, mediation path withheld and every enumerated bypass
  attempted: R cannot cause the target effect.

**What this establishes, stated precisely:** that *the mediator's credential custody* was necessary
for R to reach the effect, under this deployment and this threat model.

**What it does not establish:** that *Siphonophore* was necessary. Substituting any other program
that held M's credential and created the Pod would produce the same World-A outcome. The
intervention varies credential custody; it does not vary Siphonophore.

Siphonophore's actual contribution is *inside* the custody boundary: given that M will act only on
a valid, permitted, artifact-bound `Decision` minted by its own `Gate`, Siphonophore supplies the
authorization semantics governing what M does with the credential it holds. **The deployment
enforces necessity by credential custody; Siphonophore supplies the authorization semantics within
it.** That division is not a hedge — it is very likely the actual result of this experiment, and
naming it in advance is the reason to pre-register.

## Formal statement, and its limits

Let `Reach_T(P, A)` be the set of substrate effects principal `P` can bring about with authority
`A` via the paths enumerated in threat model `T`. Let `E` be the target effect, `R`/`M` the
requester/mediator, `A_R`/`A_M` their authorities, `D` the deployment configuration.

The property this experiment gathers evidence for:

```
    E ∉ Reach_T(R, A_R)                                          (bypass resistance)
  ∧ E ∈ Reach_T(M, A_M)                                          (the effect is reachable at all)
  ∧ ( M produces E  ⟹  ∃ d : Gate_M.verify(d) ∧ d.permitted
                          ∧ d.artifact_digest = sha256(code) )   (internal mediation enforcement)
```

all three under `⟨T, D⟩`.

Three distinct notions must not be collapsed:

- **Observed reachability** — what R was measured to be able to do over the enumerated paths.
  *This, and only this, is what a finite experiment produces.*
- **Architectural reachability** — what R could do over all paths the architecture admits. Larger
  than the enumeration; approached only by adding cases.
- **Proven impossibility** — that no path exists. **Not obtainable by experiment at any sample
  size.** `E ∉ Reach_T(R, A_R)` above is shorthand for "no enumerated path in `T` succeeded," never
  for "no path exists."

The notation is used only because it makes that three-way distinction hard to blur in prose. If a
later reviewer finds it obscuring rather than clarifying the system, the prose statements above are
authoritative and the notation should be dropped.

## Explicit non-claims

This experiment, even at full success, would **not** establish:

- Causal necessity of Siphonophore for the observed effect.
- Non-bypassability in any absolute or unqualified sense.
- Non-bypassability as a property of Siphonophore-the-SDK. It is a property of a deployment, and
  the SDK cannot supply it alone.
- Resistance to a compromised mediator process, a compromised Gate secret, or a compromised
  substrate.
- Anything about managed Kubernetes, other container runtimes, other cgroup drivers, other CNIs, or
  any topology other than the one tested — Stage 2's limits carry forward unchanged.
- Production readiness or operational reliability.
- Replay resistance, `Authority` expiry, or `Authority` revocation.
- That AgentWatch is a Siphonophore component or dependency; it remains external, unmodified, and
  used only as an independent observer.
- Any authorization decision anywhere depending on an AgentWatch observation. None is proposed.

## Open questions this design does not resolve

- Should the mediator's own substrate authority be narrowly RBAC-scoped, and does doing so change
  the claim's shape from "R cannot reach E" to the stronger "no principal in this deployment can
  reach effects outside a bounded set"?
- Does F-05 (the auto-mounted ServiceAccount token in a mediated Pod) constitute a real bypass on
  the tested cluster, and if so, is `automountServiceAccountToken: false` a Siphonophore core
  concern or a deployment concern? This is the one item that could plausibly turn into a core
  change, and the answer is deliberately not presumed here.
- Is there a defensible in-SDK notion of mediation enforcement at all, given that any in-process
  caller can bypass a library? (`contracts/spawn_helper.md` SH-23 suggests the answer is
  structurally no, and that this is correct rather than a gap.)
- Does the distinction between request identity, attempted-execution identity, and realized
  execution identity — the `execution_id`/`intent_id` question carried forward unresolved from
  Stage 1 and Stage 2 — become load-bearing once multiple concurrent requesters share one mediator?
- Where should the boundary between "SDK guarantee" and "deployment guarantee" be documented
  canonically, if this experiment confirms the split?

## Relationship to Stage 2

| | Stage 2 (complete) | This experiment (pre-registered) |
|---|---|---|
| Question | Can independent observers see the *same* execution Siphonophore claims? | Can the requester cause the execution *without* Siphonophore? |
| Result type | Correlation across evidence domains | Reachability under an authority boundary |
| Requester credential | **Held** a worker kubeconfig (needed for observation) | **Must not hold one** (that is the property) |
| Audit `user.username` | Non-discriminating (both `kubernetes-admin`) | **Discriminating** — the key field |
| eBPF | Required (kernel-execution leg) | **Not required** — dropped with reasons |
| AgentWatch role | Independent observer, unmodified | Same, narrower (audit only) |
| Siphonophore core changes | None | None expected |
| Deployment topology | Single identity, direct cluster access | **Two identities, credential custody separated** |

Stage 2 is a strict prerequisite: it established that the mediated execution is independently
observable at all. Without that, the negative results here would be uninterpretable — one could not
distinguish "the bypass failed" from "the observation machinery cannot see this kind of execution."

---

*Pre-registered design only. No experiment has been run, no cluster created, no result obtained.*
