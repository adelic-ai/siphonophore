# siphonophore — an SDK for mediated, attributable agent harnesses

v1 (archived: `archive/v1-mediation-orchestrator/`, tag `archive/v1-mediation-orchestrator`) built a
`MultiAgentBase` orchestrator that sat alongside Strands, giving *severed* sub-agents real OS
identity while everything else stayed exactly as Strands already worked. It was real and validated
on colima. It was also answering a narrower question than the one that matters: "how do we retrofit
process isolation onto Strands' existing extension point," not "what does an agent harness look
like if it's designed for attribution and audit from the start." This document is the second
question, revised twice since v1: once to generalize from "orchestrator" to "SDK" (§0), once to drop
Strands as a dependency entirely, keeping it only as a design reference to study, not import (§0).

## §0 — Study Strands, don't depend on Strands

Strands is a mature harness-construction SDK: model/provider abstractions, tool handling, hooks,
MCP support, multi-agent orchestration. Worth learning from directly, checked against the actual
installed source, not assumed — confirmed while writing this revision that `Agent.as_tool()`
(`agent/_agent_as_tool.py`) now exists, letting a sub-agent appear in a parent's `tools=[...]`
registry like any other tool. That's real, useful evidence for §1 below, not a reason to depend on
Strands: reading `_AgentAsTool.stream()`, it still just calls `self._agent.stream_async(prompt)` —
same process, same uid, zero authority boundary. Strands unified the *calling syntax*; it did
nothing to the *trust boundary*. That's the whole gap this project exists to close, now confirmed
with real code instead of inferred from the outside.

Depending on Strands as the cognitive runtime would mean trusting all of Strands' own machinery —
telemetry, session/memory managers, its own tool-execution paths, boto3/botocore transitively — not
to produce an effect outside whatever mediation gate wraps it. That's a wide, someone-else's-code
surface to reason about for a project whose entire premise is not trusting things by default.
Owning a minimal cognitive loop instead — smaller than Strands' own, since it only needs to do one
job — means there is nothing else in the trusted computing base to account for. Strands stays a
design reference (how does a mature SDK structure models, tools, hooks, MCP, context) and,
eventually, an optional adapter package, never a dependency of `siphonophore-core` or
`siphonophore-harness`.

## §1 — The one architectural move everything else follows from

Caging the Agent's Ring 4 (credential injection: the agent never holds a raw credential, it sends
an intent + identity to a gateway, which injects the real credential mid-flight) and MCP's
client-server split (tools live behind a protocol boundary, not as in-process calls) are the same
move, applied to two different things. A harness that applies this move to tool calls but not to
delegation — Strands' own shape, confirmed above even after `Agent.as_tool()` unified the calling
syntax — reintroduces exactly the same-process attribution gap one layer up. It isn't two problems;
it's the same missing move, unapplied in one place.

**So: one uniform mediation gate for every effect-producing action — tool call, sub-agent
delegation, external resource fetch.** The cognitive loop never executes or holds anything directly.
It emits an intent:

```
Principal → Intent → Mediation (Gate) → Authority decision → Execution identity → Effect → Independent evidence
```

The real question a harness like this has to answer isn't "how does an agent invoke a capability" —
it's **under what independently attributable authority may an intent become a real-world effect.**
That's the architectural center, and it's a different, harder question than invocation ergonomics,
which is all `Agent.as_tool()` actually solved.

**Candidate protocol for the cognitive-loop↔gate channel, not the security boundary itself: MCP.**
The gate is the reference monitor regardless of wire format; MCP is attractive because it can
structurally force every intent through a typed request rather than a bare function call, but the
architecture has to survive swapping MCP for something else. Worth prototyping early since it
shapes what "the harness" even is — mostly a gate implementation, with the cognitive loop reduced to
whatever's minimally needed to call a model and speak the protocol back.

## §2 — Execution class follows authority, not capability type

Not "tools stay in-process, severed sub-agents get a uid" (v1's binary) — every intent gets a
computed execution class from the same policy decision:

```
intent → policy → required authority/consequence → execution class:
    same process | separate process | uid+cgroup | container | VM
```

A pure calculation can run in-process. A public network fetch might want a constrained worker. A
repository write might need uid/cgroup separation. Something touching real credentials might need a
container or VM plus short-lived credential injection. The variable is *what this specific intent
needs*, never whether the calling code happened to name it a "tool" or an "agent" — which is also
where Beyond Detection's proportionality gets a concrete home in the architecture, not just in how
the team scrutinizes its own trust boundaries (§4).

## §3 — Two channels, not one — self-report and ground truth are different claims

Conflating them is the exact residual risk Roblox's own *Caging the Agent* talk disclosed: OTEL
self-reported by the agent, nothing reconciling it against unforgeable ground truth.

- **Self-report** — the agent/runtime's own narrative of what happened. Useful for explainability,
  worthless as ground truth alone. Tamper-resistant, not tamper-*proof*, via append-only semantics
  at the OS level (`chattr +a`, kernel-enforced — the writing process can extend but never rewrite
  or delete a prior entry without `CAP_LINUX_IMMUTABLE`, which the agent's own uid shouldn't hold).
- **Ground truth** — established by an observer outside that trust domain entirely: cgroup/uid
  accounting, network egress seen from outside the boundary, a check-in protocol verified by the
  kernel (v1's `SO_PEERCRED` mechanism is real, validated, reusable raw material here — one gate-side
  tool, not the headline of the whole project the way it was in v1).

Stable correlation identifiers (`principal_id`, `agent_id`, `intent_id`, `parent_intent_id`,
`execution_id`, `pid`, `uid`, `cgroup`) let both channels be joined *after* being produced
independently — reconciliation happens above the two claims, never inside either one. Belnap-style
four-valued comparison gives that join a precise vocabulary instead of a single fuzzy "match/no
match":

```
claimed  observed
  T         T      → corroborated
  T         F      → contradiction / unsupported claim
  F         T      → unreported activity
  F         F      → no evidence
```

## §4 — Trusted Enough to Run: a build-process pillar, not a one-time audit

Elad Meged's method (Black Hat USA 2026): list what's marked safe, find what consumes it with more
authority than that trust actually justifies, find the handoff that never gets re-checked. This
governs how the harness itself gets built, at every trust boundary, every stage — not applied once
at the end. No component is "done" until its own trust boundaries are named explicitly: what does
this piece assume is safe, what downstream code relies on that assumption with more authority than
it earns, where's the handoff nothing independently re-verifies. v1 found two real bugs this way
after the fact (a check-in socket with the wrong permissions; a nonce leaking via a world-readable
`/proc/<pid>/cmdline` instead of the inherited-fd channel it was supposed to use) and a third,
never-tested claim (cgroup-escape resistance, confirmed only on a retroactive pass). The lesson
isn't "go find more bugs" — it's that this check belongs *before* a component is called finished.

**Paired with Beyond Detection's proportionality — named everywhere, fixed proportionally.** Every
trust boundary gets named; that part is non-negotiable. The depth of the fix scales with what's
actually at stake. The gate's own core guarantee — can an intent reach an effect without passing
through mediation — gets maximum rigor and real adversarial tests. A narrow, low-consequence
boundary gets named and honestly assessed, not necessarily fixed immediately.

## §5 — Where warden/agentwatch fits: not built in, structurally compatible

"Warden built into the harness, not a dependency" doesn't hold up — warden's value *is* that the
observer sits outside the trust domain being observed (the vantage container-in-VM shape, eBPF from
a more-privileged kernel above). Collapsing that boundary turns independent verification back into
self-report. What's achievable instead: when the gate does provision a separate execution identity
(§2), it should produce exactly the OS-level shape (real uid, real cgroup) warden's existing
observation machinery already knows how to watch — so pointing warden at this harness needs zero
harness-specific adapter code, because the harness speaks the same primitives warden already watches
for everything else, not because warden's code runs inside it.

## §6 — Shape: SDK first, developed through a reference harness, no Strands anywhere in either

```
1. Define core contracts
2. Implement minimum SDK
3. Build a tiny reference harness proving the central invariant
4. Discover which abstractions were wrong
5. Refine the SDK
6. Expand the reference/default harness
```

SDK and reference harness co-evolve — don't spend months on an abstract SDK before anything runs.
Rough package shape (deliberately provisional; §4's own discipline applies to this list too — it
should survive contact with real code, not be treated as settled because it's written down):

```
siphonophore-core/
    identity/      Principal, ExecutionIdentity
    intent/        Intent, Effect
    policy/        Authority, Decision
    mediation/     Gate
    execution/     Executor, ExecutionClass   (§2)
    audit/         SelfReport, Observation, Reconciliation   (§3, Belnap comparison)

siphonophore-harness/
    a minimal native cognitive loop (prompt → completion → parse intent → feed back)
    default policy, default executors, default audit wiring, secure defaults
```

Notably absent from `siphonophore-core`: `Agent`, `Model`, `Prompt`, `Conversation`, any LLM
provider, any general reasoning loop. Those live in `siphonophore-harness` as the *reference*
implementation, not the core — a different harness (or, later, an optional adapter package studying
Strands' own patterns without importing Strands) should be able to sit on `siphonophore-core`
without carrying `siphonophore-harness`'s specific cognitive loop.

**Required invariants vs. customizable mechanisms** — extensibility must not make "Siphonophore"
meaningless. Customizable: the cognitive loop itself, the policy engine, the executor/substrate
backend, the credential broker, the observer, the protocol between loop and gate. Not customizable,
if a harness wants to claim Siphonophore conformance: an effect requires an Intent; an Intent has an
attributable Principal; the effect crosses the Gate; the Gate records a decision; execution receives
an identity; audit persistence sits outside agent authority; ground truth stays independently
observable. There should be no easy equivalent of `harness.disable_gate()` that still claims the
same guarantees — required interfaces should make the invariant hard to accidentally bypass, and a
conformance test suite (`test_no_direct_effect_path`, `test_agent_cannot_read_persisted_audit`,
`test_observer_is_outside_agent_domain`, ...) should be the actual arbiter, not documentation prose.

## §7 — First prototype: the smallest thing that proves the central claim

Not the whole SDK. Not MCP, every credential type, every substrate tier, warden integration,
multiple agents, or every policy — one vertical slice:

```
minimal cognitive loop → typed Intent → Gate
    → assign intent_id, identify Principal, make policy Decision, assign ExecutionIdentity
    → uid/cgroup Executor → Effect
```

The crucial proof: **the cognitive loop cannot produce that effect except through the gate.** Then
add a second intent shape — delegation — through the exact same path, and confirm it reduces to the
same primitive a tool call does. If both do, the central architectural claim (§1) is demonstrated,
not just asserted. Formalize into the public SDK API only after that.

## Explicitly open, not yet resolved

- The gate↔cognitive-loop protocol (MCP-native vs. something narrower) — a real decision, §1 doesn't
  settle it.
- Reconciliation mechanics beyond the Belnap vocabulary in §3 — the comparison logic itself isn't
  designed yet.
- The org/firm layer above an individual human principal (surfaced in v1's STATUS.md, still
  unaddressed).
- Whether "in-process" is ever a safe default execution class, or whether §2's policy should require
  an explicit, justified exception to stay in-process rather than treating it as the default the way
  Strands (and v1) did.

## Status

Design only. v1's code is archived, not deleted — its OS-level primitives (uid+cgroup provisioning,
the check-in protocol) are real, validated raw material this design expects to reuse inside the
Executor layer (§2, §6). What changed across both revisions is everything above the primitives:
first from a delegation-specific orchestrator to a uniform gate, then from "built alongside Strands"
to "no Strands dependency anywhere in core or the default harness."
