# siphonophore — an execution-security SDK, with a reference harness

An execution-security SDK — and a minimal reference harness built from it — for constraining every
effect-producing action to pass through a single authority boundary and remain independently
attributable. Not a framework for building agents faster, and not a general-purpose
agent-development SDK: the reference harness demonstrates the architecture, it is not what the
architecture is about. See `HISTORY.md` for how this design was arrived at and what's been learned
building toward it; this document states only the design itself.

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
no process handle, no reference to another agent. It emits an Intent. The Gate is the only thing
that ever produces an effect, and the question it exists to answer is not "how does an agent invoke
a capability" but **under what independently attributable authority may an intent become a
real-world effect.**

The protocol carrying an Intent from the cognitive loop to the Gate is a candidate mechanism, not
the security boundary itself — MCP is attractive because it can structurally force every intent
through a typed request, but the Gate is the reference monitor regardless of wire format, and the
architecture must survive swapping the protocol for something else.

## §2 — Execution class follows authority, not capability type

Execution class is a per-intent policy decision, never a property of whether the calling code
happened to name something a "tool" or an "agent":

```
intent → policy → required authority/consequence → execution class:
    same process | separate process | uid+cgroup | container | VM
```

A pure calculation can run in-process. A public network fetch might want a constrained worker. A
repository write might need uid/cgroup separation. Something touching real credentials might need a
container or VM plus short-lived credential injection. The determining variable is what the
specific intent requires, and the depth of isolation the policy grants should be proportional to
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
siphonophore-core/
    identity/      Principal, ExecutionIdentity
    intent/        Intent, Effect
    policy/        Policy, Decision
    authority/     Order, Authority, Scope   (§9)
    mediation/     Gate
    execution/     Executor, ExecutionClass   (§2)
    audit/         SelfReport, Observation, Reconciliation   (§3)

siphonophore-harness/
    a minimal native cognitive loop (prompt → completion → parse intent → feed back)
    default policy, default executors, default audit wiring, secure defaults
```

`siphonophore-core` contains no `Agent`, `Model`, `Prompt`, `Conversation`, LLM provider, or general
reasoning loop — those live only in `siphonophore-harness`, as the reference implementation, not
the core. A different harness, including one adapting patterns studied from an existing agent SDK
(never importing one — §0), should be able to sit on `siphonophore-core` without carrying
`siphonophore-harness`'s specific cognitive loop.

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

## Explicitly open, not yet resolved

Not everything below is a near-term roadmap item. Some of these questions turned out, on inspection,
to require a more general and rigorous treatment than a single implementation like this one should
try to absorb — closing them well means answering questions about authority-to-execution binding
broader than what this specific vertical slice was built to demonstrate. This project remains a
concrete, working experimental system; the items below are named honestly as open, not as signs the
system is unfinished or being set aside.

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
