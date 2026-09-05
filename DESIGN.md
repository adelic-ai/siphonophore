# siphonophore — a platform-independent execution-security SDK

A platform-independent execution-security substrate — an SDK, with a reference harness built above
it — for carrying identity, authority, delegation, and execution-relevant provenance across the
boundary where a requested action becomes real execution, and for keeping what happened there
independently checkable afterwards. Not a framework for building agents faster, and not a
general-purpose agent-development SDK: the reference harness is a *consumer* of this architecture,
not what the architecture is about.

**The core is substrate-neutral by construction.** Concrete execution substrates — Linux/local
process, UID/cgroup, and Kubernetes today — plug in below a single boundary without redefining what
`Order`, `Authority`, `Intent`, or `Decision` mean. §10 states that invariant in full and is this
repository's authoritative statement of it; every other document here defers to it rather than
restating it.

**The scope of what Siphonophore itself enforces is stated narrowly on purpose.** Within its own
call path, no effect is produced without a Gate-minted, `Executor`-re-verified `Decision` whose
bound artifact digest matches the code about to run. Whether that path is the *only* route to a
given effect is a property of the surrounding deployment — of who holds the substrate credential —
not a property this SDK can supply alone. §10's "What each layer establishes" table is where that
line is drawn, and it is load-bearing everywhere below.

See `HISTORY.md` for how this design was arrived at and what's been learned building toward it;
this document states only the design itself.

## §0 — Study Strands, don't depend on Strands

Not a blanket zero-dependencies rule — a dependency is fine when there's a specific reason a
first-party implementation would be worse and the dependency itself is mature. What's not
negotiable: no dependency on Strands, or reuse of code from this project's own prior architecture
(even code with no external imports of its own) — either way, that means trusting someone else's
agent-execution machinery, or this project's own discarded assumptions, in a place where the whole
point is not trusting things by default.

The cognitive loop (prompt → completion → parse intent → feed result back) is owned, minimal, and
built as part of `siphonophore-harness` itself. Depending on an external agent SDK for this would
mean trusting all of that SDK's own machinery — telemetry, session/memory managers, its own tool
execution paths, its transitive dependencies — not to produce an effect outside whatever mediation
layer wraps it, which is a wide surface to reason about for a project whose entire premise is not
trusting things by default. Owning the loop means there is nothing else in the trusted computing
base to account for.

Existing agent SDKs (Strands and others) are legitimate references to study — how a mature SDK
structures models, tools, hooks, protocol support, context — never something to import or adapt
code from.

## §1 — One mediation gate for every effect-producing action

A tool call, a sub-agent delegation, and an external resource fetch are the same kind of thing:
an intent that wants to become a real-world effect. All three go through the same gate — not a
tool registry for one and a bare function call for another.

```
Principal → Intent → Mediation (Gate) → Authority decision → Execution identity → Effect → evidence, reconciled where invoked
```

The cognitive loop never executes or holds anything directly — no credential, no filesystem path,
no process handle, no reference to another agent. It emits an Intent. The question the Gate exists
to answer is not "how does an agent invoke a capability" but **under what independently
attributable authority may an intent become a real-world effect.**

**The precise scope of "every," stated here rather than left to be inferred:** within Siphonophore's
own call path the Gate is the only thing that produces an effect — `Broker.dispatch()` always mints
a `Decision` through `Gate.submit()` first, and `Executor.execute()` independently re-verifies that
`Decision` before any backend runs (`broker.py`, `execution.py`). That is a real, checked property
of this code, and it is bounded by it: a process that independently holds the substrate credential a
backend would use can reach the same effect without entering Siphonophore at all. No arrangement of
this SDK's own code changes that, because such a path never enters this SDK's code. Making a
deployment's *only* reachable path run through mediation is a credential-custody problem the
deployment solves, not one the library can — §10 names the split, and §4 is why it has to be named
rather than assumed.

The protocol carrying an Intent from the cognitive loop to the Gate is a candidate mechanism, not
the security boundary itself — MCP is attractive because it can structurally force every intent
through a typed request, but the Gate is the reference monitor regardless of wire format, and the
architecture must survive swapping the protocol for something else.

## §2 — Execution class follows authority, not capability type

Execution class is a per-intent policy decision, never a property of whether the calling code
happened to name something a "tool" or an "agent":

```
intent → policy → required authority/consequence → execution class
                                                        ↓
                                          the substrate boundary (§10)
                                                        ↓
                              the concrete backend registered for that class
```

An execution class names a *requirement*, not a substrate: policy selects it, `Decision` binds it,
and the substrate boundary resolves it to whichever backend a deployment registered for it. The
classes that exist today (`same_process`, `separate_process`, `uid_cgroup`, `uid_cgroup_checkin`,
`k8s_pod`) are a set of available requirement shapes, not rungs on a single weakest-to-strongest
ladder — see `docs/EXECUTION.md` for the per-dimension treatment and §10 for the boundary itself.

A pure calculation can run in-process. A public network fetch might want a constrained worker. A
repository write might need uid/cgroup separation. Something touching real credentials might need a
container-shaped substrate plus short-lived credential injection. The determining variable is what
the specific intent requires, and the depth of isolation the policy grants should be proportional to
that requirement — not maximal by default and not minimal by default.

Every field a Decision carries that execution dispatch branches on must be cryptographically bound
into that Decision's authorization, with no exceptions. An execution-class label, an intent kind, or
any other dispatch-relevant field that isn't bound is a field that can be forged, replayed, or
silently reinterpreted to authorize something other than what was actually granted.

**Execution class is never a proxy for how much authority a principal holds, and must not become
one.** §9 names this precisely: isolation strength (what this section governs) and delegated
authority (§9's Scope) are different questions, discovered to be different the hard way during this
project's own design of delegation — an early draft used execution-class as a delegation ceiling and
had to be corrected. A sub-agent needing *stronger* isolation than its delegator for one specific
task is not a scope violation; conflating the two dimensions would make that legitimate case
indistinguishable from a real authority overreach.

## §3 — Two channels, not one: self-report and ground truth are different claims

- **Self-report** — the agent/runtime's own narrative of what happened. Useful for explainability,
  worthless as ground truth on its own. Made tamper-resistant, not tamper-proof, by append-only
  semantics enforced at the OS level: the writing process can extend the record but never rewrite
  or delete a prior entry, a guarantee the agent's own execution identity must not hold the
  privilege to override.
- **Ground truth** — established by an observer outside the agent's trust domain entirely: process/
  uid/cgroup accounting, network egress seen from outside the execution boundary, any check-in or
  attestation protocol verified by the kernel rather than asserted by the process being verified.

Stable correlation identifiers (`principal_id`, `agent_id`, `intent_id`, `parent_intent_id`,
`execution_id`, `pid`, `uid`, `cgroup`) let both channels be joined after being produced
independently. Reconciliation happens above the two claims, never inside either one, using
[Belnap's four-valued logic](https://en.wikipedia.org/wiki/Four-valued_logic) (true, false, both,
neither) rather than a single fuzzy match/no-match — chosen specifically because collapsing "the
agent lied" and "the agent did something it never mentioned" into one boolean mismatch would throw
away exactly the distinction that matters here:

```
claimed  observed
  T         T      → corroborated
  T         F      → contradiction / unsupported claim
  F         T      → unreported activity
  F         F      → no evidence
```

An agent's own execution identity must never have read access to its own persisted audit trail —
enforced as an OS-level fact (file permissions, uid separation, a one-way write channel), not a
convention expressed only by which tools happen to be registered to it.

## §4 — Every trust boundary is named, and every fix is proportional to what's at stake

Applies to the harness's own construction, continuously, not as a one-time audit: for any component
to be considered complete, its trust boundaries must be named explicitly — what this component
assumes is safe, what downstream code relies on that assumption with more authority than it earns,
and where the handoff is that nothing independently re-verifies.

Naming every trust boundary is non-negotiable. The depth of the resulting fix is not uniform — it
scales with what's actually at stake if that boundary is wrong. The Gate's own core guarantee (can
an intent reach an effect without passing through mediation) warrants maximum rigor and real
adversarial testing. A narrow, low-consequence boundary warrants being named and honestly assessed,
which is sufficient on its own without requiring an immediate fix.

## §5 — External ground-truth observers stay external, by construction

An observer that independently verifies what a harness-governed process actually did (OS-level
process/network/filesystem observation, from a more-privileged vantage point) must never run inside
the same trust domain as what it observes — collapsing that boundary turns independent verification
back into self-report, regardless of how the observer's code is packaged or distributed.

What the harness owes such an observer is not integration code, but a consistent, real OS-level
shape wherever it grants a separated execution identity (§2): a real uid, a real cgroup, the same
primitives any external OS-level observer already knows how to watch for any other process on the
host. An external observer should need zero harness-specific adapter code to watch a
harness-governed execution identity.

## §6 — Shape: an SDK first, developed through a reference harness

```
1. Define core contracts
2. Implement minimum SDK
3. Build a tiny reference harness proving the central invariant
4. Discover which abstractions were wrong
5. Refine the SDK
6. Expand the reference/default harness
```

SDK and reference harness co-evolve; the SDK is not designed in full before any of it has been
exercised by running code.

```
siphonophore_core/            the SDK — substrate-neutral throughout (§10)
    identity/      Principal, ExecutionIdentity
    intent/        Intent, Effect
    policy/        Policy, Decision
    authority/     Order, Authority, Scope   (§9)
    mediation/     Gate
    execution/     Executor, ExecutionClass   (§2)
                   ExecutionBackend — the substrate boundary   (§10)
    audit/         SelfReport, Observation, Reconciliation   (§3)

    ── substrate boundary ──────────────────────────────────────────
    concrete backends below it: Linux/local (uid+cgroup, check-in),
    Kubernetes (k8s_pod), future substrates. Substrate vocabulary
    lives here and nowhere above.   (§10)

siphonophore_harness/         the reference harness — a consumer of the SDK, not part of it
    a minimal native cognitive loop (prompt → completion → parse intent → feed back)
    default policy, default executors, default audit wiring, secure defaults
```

`siphonophore_core` contains no `Agent`, `Model`, `Prompt`, `Conversation`, LLM provider, or general
reasoning loop — those live only in `siphonophore_harness`, as the reference implementation, not
the core. A different harness, including one adapting patterns studied from an existing agent SDK
(never importing one — §0), must be able to sit on `siphonophore_core` without carrying
`siphonophore_harness`'s specific cognitive loop. That direction is one-way and structural, not a
convention: nothing in `siphonophore_core` imports `siphonophore_harness`, and an external harness
consuming `Broker.dispatch()` reaches the identical registered backend the reference harness reaches
(demonstrated for `k8s_pod` in `tests/test_harness_loop_k8s_cluster.py`, which shares one backend
*instance* across both call shapes rather than two identically-configured ones).

**The reference harness being a consumer does not make it disposable.** It is where the SDK's
abstractions get exercised by something with a user in front of it, and its usability is a
first-class engineering concern of this project (§6's own step 6, "expand the reference/default
harness") — not scaffolding kept alive only to run tests. What is not negotiable is the direction of
the dependency: harness concepts never migrate downward into the core to make the harness easier to
write.

**Required invariants vs. customizable mechanisms.** Customizable without losing Siphonophore
conformance: the cognitive loop itself, the policy engine, the executor/substrate backend, the
credential broker, the observer implementation, the protocol between loop and gate. Not
customizable: an effect requires an Intent; an Intent has an attributable Principal; the effect
crosses the Gate; the Gate records a Decision; execution receives an identity; audit persistence
sits outside the agent's own execution authority; ground truth stays independently observable.
There must be no equivalent of a flag that disables the Gate while still claiming these guarantees —
required interfaces should make the invariants structurally hard to bypass, and a conformance test
suite is the actual arbiter of whether a given harness satisfies them, not documentation prose.

## §7 — What the first working prototype must prove

Not the whole SDK, not every protocol, credential type, substrate tier, or policy — the smallest
vertical slice that proves the central invariant:

```
minimal cognitive loop → typed Intent → Gate
    → assign intent_id, identify Principal, make policy Decision, assign ExecutionIdentity
    → Executor → Effect
```

The proof required: **the cognitive loop must be structurally unable to produce that effect except
through the Gate.**

Read that at its actual scope, which §10 states in general: it is a property of the reference
harness's `CognitiveLoop` — enforced by static analysis in `test_harness_structural_proof.py`, which
checks that the loop and its neighbours import no effect-producing stdlib module and that
`CognitiveLoop.__init__` accepts nothing beyond `model`, `broker`, `principal_id`, `authority`. It is
a real structural property of that class, not a claim about arbitrary code running in the same
process, which no library can make.

An earlier version of this section additionally required that delegation be demonstrated "reducing
to the exact same primitive a tool call does, not a separately-mediated mechanism." That framing was
itself a category error, caught only after building toward it: it treated delegation as another
*Intent* shape, when an Intent is an attempted exercise of authority and delegation is the *grant* of
authority — a fundamentally different kind of operation, not a variant of the same one. Proving
"delegation dispatches through the identical call as a tool call" (as an early version of this
project's own test suite did) demonstrates repeated mediation, not delegated authority provenance —
it says nothing about whether the delegate's authority actually derives from the delegator's, is
bounded by what the delegator itself held, or traces to a real originating grant. §9 replaces this
requirement with the corrected one: **a delegated principal's authority must be demonstrated deriving
from a verified parent Authority, tracing to a real Order, never exceeding what the parent itself
could grant, checked independently at the point it's exercised** — and the exercise of that authority
still reduces to the ordinary `Gate.submit()` → `Executor.execute()` path once granted, which is the
part of the original claim that was actually correct.

**Both are now demonstrated, not merely stated as the requirement.** One composed execution
(`tests/test_harness_loop_linux.py`) exercises the corrected requirement in full: a delegated,
scope-bounded `Authority` → `Broker.dispatch()` → `Gate` re-verification → `Executor` → an
unprivileged broker crossing a narrow privileged spawn boundary (`siphonophore-spawn`) → real
uid+cgroup execution → kernel-verified check-in (`SO_PEERCRED`) → reconciliation against an
untrusted self-report. The negative cases are part of the proof, not a separate concern: scope
expansion is refused, artifact substitution is refused before the privileged boundary ever runs,
and a genuinely authentic identity's false self-report still refuses to reconcile as confirmation.
**Now also demonstrated with two independently running `CognitiveLoop` instances**, not only a
single test actor exercising delegated authority directly: `CognitiveLoop` gained an optional
`authority` parameter, threaded straight to `Broker.dispatch(intent, authority=...)` — an inert
value object, not a new capability (see `loop.py`'s own docstring for why this doesn't add a
second path to an effect, and `test_harness_structural_proof.py`'s updated signature check). Loop
B's own model-produced completion, not a directly-constructed `Intent`, is what reaches the Gate
here — this is what makes delegation visibly agent-to-agent. Granting authority
(`issue_order`/`grant_root_authority`/`delegate`) still requires a `Gate` reference `CognitiveLoop`
never holds — the orchestrating code (a test, or eventually a real harness-level component)
performs those calls and constructs each loop with the `Authority` it should hold, exactly as
DESIGN.md's own delegation model already required. See §9's "Explicitly open" notes for what
remains genuinely open in this area.

## §8 — Platform integrity is out of scope

Everything in §2 and §3 — a provisioned uid, a cgroup, a check-in protocol verified by the kernel —
only means anything if the kernel doing the verifying is itself trustworthy. Those mechanisms
establish *which process, on this host, did this* on a given occasion. They cannot establish
anything about the host itself — a compromised kernel can lie about uids, cgroup membership, and
`SO_PEERCRED` just as easily as a compromised agent can lie about what it did. This is a different
question, at a different granularity, and must not be conflated with per-execution identity.

Nothing in this project establishes or verifies platform integrity, and nothing verifies the
integrity of the broker process itself — the process holding the `Gate`'s signing secret. That
process is this system's actual trust root: every cryptographic re-verification, every
kernel-checked identity, and every reconciliation this design performs is sound conditional on that
one process not having been compromised. §4's "every trust boundary is named" discipline applies
here too — this boundary is named, not closed. Establishing platform or broker integrity
independently of the process itself is a real, harder problem this project does not attempt to
solve; it belongs to a different, lower layer than the authority-to-execution mediation this design
covers.

## §9 — Order, Authority, Scope: delegated authority is a distinct model from execution requirements

An Intent is an attempted exercise of authority, never its source. Delegation — a principal
deriving constrained authority for another principal — needed its own, first-class representation
once it became clear that treating it as "another Intent kind, Executor-handled like any other" was
where the category error in §7's original framing came from: an Intent/Decision's lifetime is one
attempt, one `intent_id`; authority is a standing thing a principal holds, that can itself become the
parent of a further, narrower grant. Conflating the two shapes onto `Decision`/`Intent` (an earlier
draft of this design bound `parent_intent_id`/`root_intent_id` directly onto `Decision`) was tried
and discarded for exactly this reason.

**Order** — the ungrounded root of a chain: the originating authorization and its issuer. Not an
Intent; it doesn't attempt an effect, it's what makes attempting effects possible at all. `issuer` is
an asserted string (an operator identity, a ticket reference), with the same disclosed-limitation
shape `Intent.consequence` already has — not independently authenticated by this model.

**Authority** — a standing, principal-scoped capability, derived either directly from a verified
Order (`Gate.grant_root_authority`) or from a verified parent Authority (`Gate.delegate`). Both
minting operations independently re-verify their input before proceeding — Gate never trusts that
some caller already checked a parent Authority or Order, the same discipline `Executor.execute()`
already applies to every Decision it's handed.

**Stated explicitly, not left implicit: an Authority is a reusable bearer capability with no
expiry, revocation, or consumption semantics.** This is a distinct property from the replay
protections that exist elsewhere in this system, on different objects, and the two are easy to
conflate given how much "one-shot" vocabulary appears nearby: a `Decision` authorizes one specific
Intent, once; `siphonophore-spawn`'s `SH-23` permits at most one real OS spawn per `execution_id`.
Neither says anything about the `Authority` behind them — `Gate.submit()` can mint an unbounded
number of further Decisions from the same Authority, for different Intents, indefinitely, and
`Gate` is deliberately stateless (no ledger — see §9's own framing above), so no mechanism here
could track single-use even if it were intended to. A leaked delegated Authority (captured from
logs, a compromised sub-agent process, or anywhere else) remains fully exploitable within its
Scope indefinitely. This is not mitigated by anything `Gate`/`Authority` currently do — narrowing
this is an orchestration-layer design concern (short-lived processes holding narrow scopes), named
here rather than assumed solved.

**The precise guarantee a delegated Authority's `order_id`/`parent_authority_id` fields carry, stated
narrowly rather than oversold:** they attest that *Gate*, at the moment it minted this Authority,
independently verified the parent and confirmed the derivation rules (subset scope, remaining
delegation depth) held against it. This is not the child Authority independently reconstructing or
re-proving the entire ancestry chain from its own fields in isolation — that stronger property would
require each hop to be checkable without trusting Gate's own minting discipline (e.g. independent
per-link signatures, as in a macaroon scheme). This system has exactly one Gate mediating every hop
with one secret; by induction, the chain is sound as long as Gate's re-verify-before-mint discipline
held at every step that produced it — a real, meaningful guarantee, just a different claim than
self-proving-without-Gate, and one worth stating precisely rather than letting "cryptographically
bound" imply more than it does.

**Scope** — what an Authority actually permits and how much further it may be delegated. Deliberately
minimal, its first representation on purpose rather than by oversight: `allowed_kinds` and a
`remaining_delegation_depth` budget. No per-payload or per-resource constraints (e.g. "may
`write_file` only under `/tmp`") — real, plausible future need, not built speculatively ahead of
actual pressure to build it. No isolation/execution-strength dimension at all, ever — see §2's note
above for why that's load-bearing, not incidental.

**What this does not attempt to be:** a general IAM framework, a Zanzibar/Warrant-style relationship
system, a policy language, or an organization hierarchy above individual principals. `principal_id`
remains a bare string throughout, matching every other use of it in this design — Order/Authority
narrow what a principal may do, they don't model who or what a principal *is*.

`Gate.submit()` gained an optional `authority` parameter for this: omitted, behavior is unchanged
from before this section existed (an authority-less submission, evaluated purely by `Policy`,
`Decision.authority_id`/`order_id` both `None`). Given, three independent checks run before policy
is consulted: the Authority itself re-verifies; `intent.principal_id` must match
`authority.principal_id` (without this, a leaked/observed Authority object — a bearer capability —
could be used to submit on a different principal's behalf); `intent.kind` must be in
`authority.scope.allowed_kinds`. The first two are structural-mismatch failures (`GateViolation`,
nothing minted); the third folds into `permitted` alongside the ordinary policy result — a real,
signed, auditable "no," the same shape any other policy denial already takes.

## §10 — Platform independence: a substrate-neutral core, one boundary where substrates plug in

This section is the authoritative statement of the invariant the rest of this repository defers to.
It exists because the failure mode it guards against is quiet: a concrete substrate is added, its
native vocabulary is convenient, and within a few commits the general architecture has silently
become that substrate's architecture.

```
        reference harness              external harnesses
                    \                   /
                     \                 /
                 Siphonophore SDK / core        ← substrate-neutral, always
                             |
                  ── execution substrate boundary ──        (ExecutionBackend)
                             |
              +--------------+---------------+
              |                              |
        Linux / local                   Kubernetes
   same_process, separate_process,        k8s_pod
   uid_cgroup, uid_cgroup_checkin            |
                                     future substrates
```

### The invariant

**Core concepts do not acquire substrate vocabulary.** `Order`, `Authority`, `Scope`, `Intent`,
`Effect`, `Policy`, `Decision`, `Gate`, and `Executor` mean exactly the same thing regardless of what
executes below them. Adding a substrate must require no change to any of them.

This is checked, not asserted. `tests/test_core_no_k8s_vocabulary.py` scans every module in
`siphonophore_core` except the one backend allowed to know what a Pod is, in two layers: a
prose/reference scan for `Pod`/`Job`/`Namespace`/`ServiceAccount` as standalone capitalized words and
for `Kubernetes`/`kubectl`/`k8s` case-insensitively, plus an AST scan of dataclass field names and
function parameter names against a Kubernetes-noun list — the second layer specifically because a
lowercase `namespace: str` field added to a core dataclass is the realistic leak shape and the regex
alone would pass it silently. A new substrate should extend the same discipline rather than opt out
of it.

### The substrate boundary

`ExecutionBackend` (`execution.py`) *is* the boundary. Above it: authorization, decision minting,
independent re-verification, artifact binding, execution-class selection. Below it: whatever it
actually takes to make the effect happen on one concrete substrate.

`Executor.execute()` performs, in order, decision↔intent correspondence, `gate.verify()`,
`decision.permitted`, and the artifact-digest comparison — **and only then** looks up a backend.
Backends are handed an already-verified `Decision`; they do not re-authorize, and are not the layer
that could. No backend in this repository reads more than one field off the `Decision` it is given:
the four substrate-identity backends and `K8sPodBackend` read `decision.intent_id` (their execution
correlation identity) and nothing else, and the two portable backends read no `Decision` field at
all. That factoring is correct and deliberate, and it has
a consequence worth stating plainly rather than discovering later: **a backend invoked directly,
outside `Executor`, will act on any `Decision`-shaped object it is handed**, because checking is not
its job. Authorization belongs above the execution substrate (the same principle
`contracts/spawn_helper.md`'s `SH-23` states for the privileged helper), and what keeps a caller on
the authorized path is the deployment's credential custody, not the backend's own scepticism.

### Substrate vocabulary stays below the boundary

`Pod`, `Job`, `Namespace`, `ServiceAccount`, `kubeconfig`, `kubectl` are Kubernetes implementation
concepts. They are not Siphonophore primitives, and they do not become primitives by being useful.
Promoting a substrate-specific object into a core concept requires two things, both of them
demonstrated rather than anticipated: a genuinely substrate-neutral formulation, and a concrete need
from more than one substrate that the current core cannot express. Absent that, the concept stays in
its backend, exactly as `pod_name_for()`'s RFC 1123 name derivation and `uid_cgroup`'s own
`_EXECUTION_ID_RE` charset check both do today — each backend owning its substrate's naming rules is
the existing pattern, not an exception to one.

The distinction to keep: **the Kubernetes implementation is genuinely Kubernetes-native; Siphonophore
itself remains Kubernetes-independent.** A new engineer should be able to read this repository and
conclude that Kubernetes is a supported and important concrete substrate, and that it is not what
Siphonophore is.

### Substrates that exist today

| Substrate | Execution classes | Backends | Status |
|---|---|---|---|
| Linux / local process | `same_process`, `separate_process` | `SameProcessBackend`, `SeparateProcessBackend` | portable; refuse to run under a root broker unless `allow_root=True` |
| Linux / OS execution identity | `uid_cgroup`, `uid_cgroup_checkin` | `UidCgroupBackend`, `SpawnHelperBackend`, `CheckedInUidCgroupBackend`, `CheckedInSpawnHelperBackend` | real ephemeral UID + cgroup v2 leaf; `*_checkin` adds kernel-verified (`SO_PEERCRED`) check-in |
| Kubernetes | `k8s_pod` | `K8sPodBackend` | real Pod per execution, proven against `kind`; not in the default `Policy` mapping; no check-in tier |

Linux/local is a first-class substrate and stays one. It is the only substrate where execution
identity is currently established *independently of the executing process*, which makes it the
strongest, not the legacy, path. Sandbox/namespace-only and VM substrates are architectural
direction with no backend behind them; adding a future substrate should not require redefining
Siphonophore's core substrate-neutral semantics merely to accommodate its own vocabulary or
mechanics — it may still legitimately require substrate-specific backend configuration, policy
mapping, deployment components, or evidence/correlation mechanisms of its own, the same way
Kubernetes needed `pod_name_for()`'s own naming rules and gained no equivalent of a check-in tier.

### What each layer establishes — and what it does not

Verification, observation, correlation, and attribution are four different things, and a claim that
does not name the layer supporting it is not yet a claim. This table is the reference for the rest of
the documentation.

| Property | Established by | Explicitly not established |
|---|---|---|
| **Authorization semantics** — whether this intent, under this authority, is permitted, and under which execution class | `Gate.submit()` (§9) and `Policy`; bound into `Decision`'s HMAC | that the caller had no other way to reach the effect |
| **Internal mediation enforcement** — no `Effect` from Siphonophore's own path without a verified, permitted, artifact-bound `Decision` | `Broker.dispatch()` → `Gate` → `Executor.execute()` re-verification | anything about a caller who never calls `Broker.dispatch()` |
| **Substrate-authority custody** — who can reach the substrate at all | the surrounding deployment: OS identities, credential placement, RBAC, sudoers | nothing in this SDK; backends consume whatever ambient authority their process already holds |
| **Execution identity** — which OS-level identity actually ran | `uid_cgroup_checkin`'s kernel-verified (`SO_PEERCRED`) check-in, established independently of the executing process | the same independence for any class without check-in: plain `uid_cgroup` provisions an identical real UID and cgroup but reads that identity from `/proc`, in the process asserting the rest of the chain; `k8s_pod` has no check-in tier at all |
| **Independent observation** — that some external vantage saw the execution | an observer outside the trust domain (§5), on its own evidence | that Siphonophore caused it, or that no other path existed |
| **Correlation** — that separately-produced records concern the same execution | shared identifiers, joined above both channels (§3) | that agreement between channels implies either one is complete |
| **Attribution** — which principal an effect is ascribed to | reconciliation (§3) over the above, never stronger than the execution identity policy actually provisioned | agent-granular attribution where no distinct execution identity was provisioned |

Three consequences follow, and none of them is a hedge:

- **Independent observation is not mediation.** An observer establishing that an execution happened,
  and that its records agree across vantages, says nothing about whether it *had* to go through
  Siphonophore. Correlation is not causation here in the plain, literal sense.
- **No finite experiment establishes universal non-bypassability or causal necessity.** An experiment
  enumerates paths; it cannot quantify over all of them. Documentation in this repository states
  what was reached over an enumerated set of paths in a named topology, and stops there.
- **ALLOW evidence and DENY evidence are not symmetric.** A permitted execution leaves a concrete
  object to point at from several vantages. A refusal leaves an absence, and an absence is bounded by
  the window, scope, and identifier the observer had available — sometimes there is no surviving
  correlation identifier at all, because the refusal happened before one existed. Say which of the
  two is being reported.

### Where this invariant is enforced, and where it merely holds

Checked by tests: core vocabulary neutrality (`test_core_no_k8s_vocabulary.py`), the reference
harness's structural inability to reach an effect except through `Broker`
(`test_harness_structural_proof.py`), and both call shapes reaching one shared backend instance
(`test_harness_loop_k8s_cluster.py`). Held by discipline, not by a test: that a future substrate does
not quietly widen a core concept to fit its own vocabulary. That is what this section is for.

The corresponding project-level statement — what is actively being built, and what is deliberately
not being reopened — lives in `README.md`'s "Project status and current direction", which is the
single canonical answer to "what is this project working on now."

## Explicitly open, not yet resolved

Not everything below is a near-term roadmap item. Some of these questions are broader than this
project's own engineering scope, and are deliberately left open here rather than answered by
enlarging Siphonophore to reach them — a general treatment of authority-to-execution binding is not
what this SDK is for. The items below are named honestly as open, not as signs the system is
unfinished or being set aside. Where one of them would actually block the current engineering
direction, it is called out as such; the rest are docketed.

- The gate↔cognitive-loop protocol (MCP-native vs. something narrower) — §1 names it as a
  candidate, not a decision.
- §3's reconciliation compares exactly two booleans (claimed, observed) via plain equality — richer
  comparisons (partial matches, ambiguous claims, or treating check-in success/failure as a third
  independent signal alongside claimed/observed) are a real generalization this project has not
  built and is not pursuing here.
- `principal_id` is a bare string throughout, with no organization/tenant representation above an
  individual principal, and no real `Principal` class exists despite §6's module layout having
  anticipated one early on.
- §9's Scope is deliberately minimal (kind-membership and delegation-depth only) — per-payload or
  per-resource delegation constraints (e.g. "may write_file only under a specific path") are real,
  plausible future need, not yet justified by anything actually built that needs them.
- An `Authority` has no expiry, revocation, or consumption semantics — it's a reusable bearer
  capability for as long as its `Scope` remains meaningful, distinct from the replay protections
  `Decision`/`SH-23` provide for other objects. See §9's own fuller explanation above. Narrowing
  this (short-lived Authorities, explicit revocation) is real future work, not yet built or
  justified by anything this project has needed so far.
- §9's Authority/Order mechanism is exposed through `Broker.dispatch(intent,
  authority=...)` — omitted, unchanged from before; given, threaded straight to
  `Gate.submit(intent, authority=authority)`. **Now also exposed at the `CognitiveLoop` level**:
  an optional `authority` constructor parameter, threaded to `broker.dispatch()` unchanged —
  `tests/test_harness_loop_linux.py` runs two independently constructed `CognitiveLoop` instances
  (separate `Model`, history, `principal_id`), sharing one `Gate`/`Executor`/`Broker`, with the
  second loop's own model-produced completion (not a directly-constructed `Intent`) reaching the
  Gate through its delegated `Authority`. `CognitiveLoop` itself still cannot grant or derive
  authority — it has no `Gate` reference, only ever exercises an `Authority` handed to it at
  construction — so `issue_order()`/`grant_root_authority()`/`delegate()` (the grant side) remain
  outside both `Broker` and `CognitiveLoop`, performed by whatever orchestrates the agents (test
  code today; a real harness-level orchestration component, not yet built, eventually). **What
  remains genuinely open:** that orchestration component itself — something that decides *when* to
  delegate, constructs the second loop, and supplies its own `Model` — doesn't exist; today's proof
  is that the mechanism composes correctly once an orchestrator (of any shape) does those three
  things, not that Siphonophore includes such an orchestrator.
- Whether "same process" should ever be a default execution class, or whether §2's policy should
  require an explicit, justified exception to stay in-process rather than treating it as a default.
- Artifact identity is currently an inline-code digest only (`digest_of()`, §2/§9) — authorizing a
  *reference* (a module path, a container image digest, a package version) instead of inline source
  is not implemented.
- Check-in and reconciliation are wired into `uid_cgroup_checkin` via two backends —
  `CheckedInUidCgroupBackend` (`preexec_fn`, requires real root) and `CheckedInSpawnHelperBackend`
  (`siphonophore-spawn`, unprivileged-broker-compatible). `same_process` and `separate_process` have
  no check-in-gated or automatically-reconciled equivalent — a current limitation of those two
  classes specifically, not something either backend or this design attempts to generalize here.
- `uid_cgroup_checkin`'s guarantee that a check-in failure cannot co-occur with an
  already-performed artifact effect is a property of its own wrapper (`_CHECKIN_CHILD_WRAPPER` gates
  `intent.artifact_code` on `perform_checkin()` succeeding first), not something the architecture
  enforces for every possible checked-in backend. A differently-shaped backend (e.g. one that
  starts the artifact concurrently with check-in for latency reasons) could produce a real,
  observed effect with a failed identity binding — `CheckinFailedError` already carries its
  `observations` for exactly this reason, but nothing today distinguishes "nothing happened" from
  "something happened, attribution is invalid" at the type level.
- A broker process that wants both the `uid_cgroup`/`uid_cgroup_checkin` tiers and the portable
  tiers available previously had to run entirely as root. This is now closed: `same_process`/
  `separate_process` refuse outright rather than silently inheriting root (`allow_root=True`
  required); `useradd`/`userdel` go through privilege separation via two self-validating wrapper
  scripts (`scripts/README.md`), validated on colima with a real sudoers grant; cgroup management
  needs only ownership delegation, no code change; and the `preexec_fn` privilege-drop step is
  closed by `siphonophore-spawn` (`contracts/spawn_helper.md`), implemented, validated on colima,
  and wired into `SpawnHelperBackend`/`CheckedInSpawnHelperBackend`. A deployment chooses which
  backend to register per execution class; `Gate`/`Executor`/`Decision` are unaware of the
  difference. Two limitations remain, both disclosed rather than silently worked around: finished
  executions' cgroup leaves are not automatically removed (an empty cgroup v2 leaf is a
  near-zero-weight kernfs entry — the cost of building safe cleanup was judged not worth it); and
  the helper cannot establish that the broker's own request was ever authorized by a real
  `Gate.submit()` call in the first place — see `contracts/spawn_helper.md`'s `SH-23` section for
  the precise statement of what the helper does and does not prove.

### Left open by the Kubernetes substrate work specifically

Named here because §10 makes them visible, not because this document resolves any of them. None is a
redesign mandate; each is a question to answer when a concrete engineering need forces it.

- **`intent_id` as execution correlation identity.** Every backend in this repository uses
  `decision.intent_id` as its execution_id, by convention — there is no dedicated `execution_id`
  concept, and none of the substrate work introduced one. This is workable for the sequential,
  single-requester shapes built so far, and `k8s_pod` already has to *derive* a substrate-legal name
  from it (`pod_name_for()`) rather than use it directly. Whether request identity, attempted-execution
  identity, and realized-execution identity are genuinely three concepts is the open question; it
  becomes load-bearing if concurrent requesters ever share one mediating process, and not before.
- **Workload-side substrate credentials.** `K8sPodBackend` sets no `serviceAccountName`, so a mediated
  Pod runs under its namespace's `default` ServiceAccount with a projected token automounted by the
  cluster's own defaults. This is a precise Kubernetes engineering consideration — described in
  `docs/EXECUTION_K8S.md`, where it belongs — and deliberately not generalized into a core concept:
  "what credential the executed workload itself holds" is the Credentials dimension `docs/EXECUTION.md`
  already names as unbuilt, and Siphonophore ties no credential delivery to an authorized execution
  today on any substrate. Whether `automountServiceAccountToken: false` should be a backend default,
  a deployment decision, or a policy-visible field is not decided here.
- **Stronger DENY / non-execution evidence.** A refusal produces an absence, and the observation work
  could only bound absences by window, namespace, and naming convention — in one shape, with no
  surviving identifier at all, because the intent_id was lost when the violation propagated before an
  `Effect` existed. Whether a refusal can be given a durable, correlatable artifact of its own, without
  making the refusal path itself effect-producing, is open.
- **Distinct substrate principals for mediation versus observation.** Whether a mediating identity and
  an observing identity must be separate substrate principals — rather than one identity conveniently
  doing both — is a real question about what an observation can be said to establish (§5, §10). It is
  a study design question, not an SDK change.
- **Audit coverage.** Substrate-level audit configuration (what the control plane records, at what
  level, for which resources) is a deployment decision that materially changes what any observer can
  establish. Nothing in Siphonophore requests, requires, or verifies it.
- **Where the SDK/deployment guarantee boundary is documented canonically.** §10's table is this
  repository's answer as of this document. A pre-registered study of the deployment half of that
  split — the `explore/k8s-mediation-bypass` branch — has been executed, scientifically closed, and
  published: 9/9 pre-registered criteria PASS, curated result at
  `experiments/k8s_mediation_bypass/RESULT.md`. Bounded conclusion: under the tested deployment and
  enumerated threat model, the requester without substrate authority could reach the target
  Kubernetes effect only through the authorized mediated path, while none of the enumerated bypass
  paths produced it. That does not establish absolute non-bypassability, universal causal necessity,
  or that Siphonophore alone prevents bypass — deployment credential custody supplied the authority
  separation the result depends on, and Siphonophore supplied authorization semantics within that
  boundary, exactly as §10's table already distinguishes. The open question this bullet names is
  narrower than the study itself: whether §10's table remains the right *canonical location* for
  that split as more such studies accumulate, not whether the split holds.
