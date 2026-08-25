# siphonophore (v2) — a harness redesigned around mediation, not a mechanism bolted onto one

v1 (archived: `archive/v1-mediation-orchestrator/`, tag `archive/v1-mediation-orchestrator`) built a
`MultiAgentBase` orchestrator that sat alongside Strands, giving *severed* nodes real OS identity
while everything else stayed exactly as Strands already worked. It was real, validated on colima,
and it's still true as far as it goes. It was also answering a narrower question than the one that
actually matters: "how do we retrofit process isolation onto Strands' existing extension point,"
not "what does a harness look like if it's designed for attribution and audit from the start."
This document is the second question.

## The one architectural move everything else follows from

Caging the Agent's Ring 4 (credential injection: the agent never holds a raw credential, it sends
an intent + identity to a gateway, which injects the real credential mid-flight) and MCP's own
client-server split (tools live behind a protocol boundary, not as in-process calls) are the same
move, applied to two different things. Strands applies that move to tool calls — a tool goes
through a registry, can be sandboxed — but not to delegation, which is a bare Python function call.
That asymmetry is the actual root of the same-process attribution gap v1 was patching. It isn't a
separate problem needing a separate mechanism; it's the same missing move, unapplied in one place.

**So: one uniform mediation gate for every effect-producing action — tool call, sub-agent
delegation, external resource fetch — not three different mechanisms (a tool registry, a
credential-injection gateway, a delegation-specific orchestrator).** The cognitive loop (the actual
LLM-calling event loop: prompt → completion → parse intent → feed result back) never executes or
holds anything directly. It emits an intent. The gate is the only thing that ever touches a
credential, a filesystem path, a process spawn, or a sub-agent. Delegating to a sub-agent isn't
special-cased — from the cognitive loop's perspective it's just a more complex intent through the
same gate every tool call already goes through.

**Candidate mechanism, not yet settled: the gate speaks MCP.** If the cognitive loop only ever
talks to the gate via MCP (the same protocol either way, whether the intent is "read this file" or
"delegate this to a sub-agent"), unification isn't just a principle, it's structurally enforced —
there is no *other* channel for the cognitive loop to reach an effect through. The gate, on its far
side, decides per-intent: run this in-process (cheap, most tool calls), spawn a severed process
with its own uid+cgroup (v1's mechanism, reused here as one gate-side implementation detail rather
than a whole parallel orchestrator), or hand it to a heavier substrate. Worth prototyping early,
since it changes what "the harness" even is: mostly a gate implementation, with the cognitive loop
reduced to whatever's minimally needed to call a model and speak MCP back.

## Two channels, not one — self-report and ground truth are different claims

Conflating them is the exact residual risk Roblox's own *Caging the Agent* talk disclosed: OTEL
self-reported by the agent, nothing reconciling it against unforgeable ground truth. A harness
designed around attribution keeps these architecturally separate, not just conceptually separate:

- **Self-report** — the agent's own narrative (what it did, why). Useful for explainability,
  worthless as ground truth on its own. Made tamper-resistant, not tamper-*proof*, by being
  append-only at the OS level (`chattr +a`, kernel-enforced — even the writing process can extend
  but never rewrite or delete a prior entry without `CAP_LINUX_IMMUTABLE`, which the agent's own
  uid shouldn't hold).
- **Ground truth** — produced by something outside the agent's process entirely: cgroup/uid
  observation, network egress seen from outside the trust boundary, a check-in protocol verified by
  the kernel (v1's `SO_PEERCRED` mechanism is real and reusable here, just no longer the whole
  point of the project — it's one gate-side tool among several, not the headline).

Reconciliation between the two is where "was this actually audited" lives — and it only means
anything if the two channels can't converge through the same trust domain.

## "Inaccessible to the model via the harness" has to be OS-enforced, not a tool-list convention

Never registering a read-the-log tool is necessary but not sufficient — an agent with *any* other
filesystem-touching capability could still read the log file directly unless the OS itself refuses.
The gate's own privileged process should own the log; the agent's uid gets write access to a
one-way channel (a socket, or a named pipe the gate is the sole reader of) and no read permission on
anything persisted. "Can't read its own audit trail" should be a fact about file permissions and
uid separation, not a fact about what's in a `tools=[...]` list that a differently-configured agent
could simply not have the same restriction.

## Smaller than Strands, on purpose

The actual cognitive loop — prompt, completion, parse intent — is small. Strands carries a lot that
a narrow, audit-first harness doesn't need: a general plugin/hook framework, multiple sandbox
backends as a pluggable system, skills, session/conversation managers, A2A protocol support,
boto3/botocore as a baseline dependency. That's Strands being a general-purpose product for many use
cases, which is a legitimate thing for Strands to be and not a reason this harness needs the same
surface area. Once tool-call and delegation are unified behind one gate, most of what Strands
carries to manage the *distinction* between them stops being needed at all.

## Trusted Enough to Run: a build-process pillar, not a one-time audit

Elad Meged's method (Black Hat USA 2026): list what's marked safe, find what consumes it with more
authority than that trust actually justifies, find the handoff that never gets re-checked. This
governs *how the harness itself gets built*, at every trust boundary, every stage — not something
applied once at the end. Concretely: no component is "done" until its own trust boundaries are
named explicitly — what does this piece assume is safe, what downstream code relies on that
assumption with more authority than the assumption earns, where's the handoff that isn't
independently verified. v1 found two real bugs this way after the fact (a check-in socket with the
wrong permissions, a nonce leaking via argv) and a third, never-tested claim (cgroup-escape
resistance) on a retroactive pass — the lesson isn't "go find more bugs," it's that this check
belongs *before* a component is called finished, not as an audit bolted on afterward once something
already looks done.

**Paired with Beyond Detection's proportionality, not run at uniform intensity everywhere.** Naming
every trust boundary is non-negotiable. The *depth* of the fix isn't uniform — it scales with what's
actually at stake if that boundary is wrong. The gate's own core guarantee (can an intent reach an
effect without going through mediation) gets maximum rigor, real tests, real adversarial thinking.
A narrow, low-consequence boundary gets named and honestly assessed, not necessarily fixed
immediately — disclosed-and-deferred is a legitimate outcome of applying the method, silence is not.

## Where warden/agentwatch actually fits — not built in, structurally compatible

"Warden built into the harness, not a dependency" doesn't hold up: warden's entire value is that
the observer sits outside the trust domain being observed (the vantage container-in-VM shape, eBPF
from a more-privileged kernel above). Collapsing that boundary turns independent verification back
into self-report. What's achievable instead: the gate's own dispatch, when it does spawn a severed
process, should produce exactly the OS-level shape (real uid, real cgroup) warden's existing
observation machinery already knows how to watch — so pointing warden at this harness needs zero
harness-specific adapter code, not because warden's logic lives inside it, but because the harness
speaks the same primitives warden already watches for anything else.

## Explicitly open, not yet resolved

- The gate's exact implementation shape (MCP-native vs. something narrower) — a real decision, not
  settled by this document.
- Reconciliation mechanics between self-report and ground truth — v1's `SO_PEERCRED`/cgroup pieces
  are reusable raw material, not a finished reconciliation design.
- The org/firm layer above an individual human principal (surfaced in v1's STATUS.md, still
  unaddressed).
- Whether "in-process" is ever actually safe as a gate-side choice, or whether the gate should
  default to at least uid separation for everything, with "stay in-process" requiring an explicit,
  justified exception rather than being the default the way it is in Strands today.

## Status

Design only. v1's code is archived, not deleted, and its OS-level primitives (uid+cgroup
provisioning, the check-in protocol) are real, validated raw material this design expects to reuse
inside the gate — the part that changed is what sits *above* them, not the primitives themselves.
