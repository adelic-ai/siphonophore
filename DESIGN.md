# siphonophore — an SDK for mediated, attributable agent harnesses

An SDK, and a reference harness built from it, for constructing agent systems where every
effect-producing action is mediated by a single authority boundary and independently attributable —
not a framework for building agents faster, a framework for making it structurally impossible for
an agent's actions to escape attribution. See `HISTORY.md` for how this design was arrived at and
what's been learned building toward it; this document states only the design itself.

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
Principal → Intent → Mediation (Gate) → Authority decision → Execution identity → Effect → Independent evidence
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
independently. Reconciliation happens above the two claims, never inside either one, using a
four-valued comparison rather than a single fuzzy match/no-match:

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
    policy/        Authority, Decision
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
through the Gate.** A second intent shape — delegation — must then be demonstrated reducing to the
exact same primitive a tool call does, not a separately-mediated mechanism. Only after both are
demonstrated, not merely asserted, does any of this get formalized into a public SDK API.

## Explicitly open, not yet resolved

- The gate↔cognitive-loop protocol (MCP-native vs. something narrower) — §1 names it as a
  candidate, not a decision.
- The reconciliation logic that actually implements §3's four-valued comparison.
- An org/firm layer above an individual human Principal — delegation chains today only reach a
  single human principal, with no representation of an organization the principal belongs to.
- Whether "same process" should ever be a default execution class, or whether §2's policy should
  require an explicit, justified exception to stay in-process rather than treating it as a default.
