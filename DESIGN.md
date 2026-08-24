# siphonophore

A `MultiAgentBase`-compatible orchestrator for Strands that gives every delegated agent a real OS
identity, a risk-tiered execution substrate, and independent ground truth — properties Strands
itself doesn't provide, because attribution/accountability across sub-agents was never the problem
it set out to solve. Not a Strands replacement or a fork of the SDK: a new orchestrator, built
against the same contract Strands' own `Swarm`/`Graph` implement, that reuses Strands' `Agent`
(model calls, tool-calling loop) as a dependency rather than modifying it.

## Why: the same-process attribution gap

Strands' own delegation model is a plain in-process function call. A parent agent invoking a
sub-agent — `summarizer(...)` in a typical `@tool`-decorated delegation function — is just Python
calling Python. No `fork`, no `subprocess.Popen`. Parent and every delegated sub-agent share one
pid *and* one uid, because they never leave the one OS process the harness is running in. Any
uid-based attribution built on top of Strands (proxy logs, egress observation, audit reconciliation
— the kind of thing [warrant](https://github.com/adelic-ai/warrant) does at the token layer)
silently assumes one uid per session, which Strands' actual delegation model doesn't give you: two
concurrently-running agents inside one process are indistinguishable at the OS level.

Checked against Strands' actual source (not assumed): `Swarm._execute_node`
(`multiagent/swarm.py:931`) and `Graph._execute_node` (`multiagent/graph.py:984`) both call
`node.executor.stream_async(...)` directly, in-process. Strands does ship a real hook system —
`BeforeNodeCallEvent`/`AfterNodeCallEvent`, with `cancel_node` giving genuine binary
enforcement — but nothing in it redirects *where* a node's execution happens. A plugin can observe
and veto; it can't make a node's work happen inside a different process. That requires the
orchestrator's own dispatch code to change, which is what this project is.

## The octopus/colony correction — not every appendage needs severing

Strands is accurately one organism, one process, many tentacles — that's not a bug relative to
what Strands is *for* (AWS's own description: "a model-driven approach to building AI agents in
just a few lines of code," a productivity/orchestration framework, not a security framework). An
octopus sharing one bloodstream across all its arms is fine because every arm carries the same
authority. A system where different appendages are deliberately given *different* authority breaks
that assumption — poison introduced through a low-authority arm (a sub-agent reading a malicious
document) can reach whatever a high-authority arm holds, because sharing one process means there
was never a wall between them, just a naming convention.

That argues for severing specific appendages into their own process, not blanket "every delegation
gets its own OS identity" — most arms in a real deployment should stay part of the shared organism,
because that's cheaper and matches how the harness wants to be used. Two axes decide which
appendages are worth severing:

1. **Authority width × lifetime** — blast radius if the process holding this appendage's
   credentials is compromised. A token scoped to one read for one call is low-stakes even fully
   compromised. A standing, long-lived delegation with consequential write/action authority (filing
   something with a court, sending an email as the principal) is not.
2. **Untrusted-binary-input exposure** — likelihood of compromise in the first place, and
   specifically compromise of the *process*, not just the LLM. Plain text that becomes LLM context
   is a prompt-injection surface, already answered by narrow tool-scoping plus whatever
   authorization layer checks token scope (Cedar, in warrant's case) — regardless of which OS
   process presented the token. Untrusted *binary* content with real parser attack surface (PDFs,
   attachments, scanned documents) is a different threat: a parser exploit achieves code execution
   *before* the LLM or any tool-scoping ever enters the picture. Narrow authority doesn't help
   there, because the compromise happens underneath it.

An appendage that's narrow-authority and only ever touches trusted/structured input can safely stay
part of the shared process. One that's broad-authority, or reads untrusted binary content, is where
severing earns its cost.

## What "severing" means, concretely

1. **Provision the process at delegation time, not organically.** A pid isn't earned by an agent
   doing something — it's assigned the instant a process is spawned, before it runs any code. The
   orchestrator's node-dispatch should provision the sub-agent's process (or heavier substrate, per
   the tier below) *before* handing it any real work, the same "provision the identity first, then
   hand off the work" shape a session-scoped uid allocator uses.
2. **Require a real, proxied check-in before that identity is trusted for anything** — not a
   self-reported label. This is closer to Kerberos than to a bare claim: neither the orchestrator
   alone (which only knows what it provisioned) nor the spawned process alone (which only knows its
   own identity) can produce the pairing "this process really is this delegation" — it takes both
   sides' independently-held pieces agreeing, verified by something with a different vantage point
   than the process being verified. A process that never checks in this way is exactly as untrusted
   as one that was never provisioned.
3. **Prefer a cgroup over a bare pid for tracking a whole descendant subtree.** A pid identifies one
   process; it says nothing about that process's own children if a sub-agent's tool later forks or
   execs further work — the same fork-gap blindness exec-only observation (auditd) has always had.
   A cgroup, assigned once to a whole process tree, is tracked by the kernel across that entire
   tree persistently, including descendants that fork later without exec'ing.

## Substrate selection, per node

The isolation tier isn't a fixed deployment-time choice — it's a per-node decision, driven by the
same two axes above:

- **Bare process + uid + cgroup**, inside a shared outer container — cheap, fast (no VM/container
  boot latency per delegation), sufficient for narrow-authority/trusted-input nodes. Gives real
  uid-enforced file permissions and uid-gated network egress; does not give pid/mount/network
  namespace isolation.
- **A real container** — full namespace isolation, worth the spin-up cost specifically for nodes
  handling untrusted binary parsing or broad/consequential authority.
- **microVM (Firecracker) or a syscall-interception sandbox (gVisor)** — heavier tiers for the
  highest-risk quadrant (broad authority *and* untrusted-input exposure), not a default.

This is also where multi-model and multi-substrate fall out for free: Strands' `Agent` is already
model-agnostic (swappable per node independent of the harness), so a node is free to pick both its
own model and its own isolation tier. One harness, one identity/attribution scheme, many models,
many substrates.

## The second reason to build this: Strands has no warden/agentwatch-ness

Checked `strands/telemetry` directly: `Tracer`, `EventLoopMetrics`, `ToolMetrics`, OTEL sent to
OTLP endpoints — all emitted from inside the agent's own process, by the same code being observed.
Identical shape to the residual risk Roblox's own *Caging the Agent* talk (Black Hat USA 2026)
disclosed: self-reported telemetry with nothing reconciling it against unforgeable ground truth.
Strands' plugin/hook system doesn't close this either — every hook fires from inside the same trust
domain being observed, so a genuine process compromise (not just a manipulated LLM) could in
principle suppress or fake what a hook reports, the same way it could fake OTEL.

This converges with the attribution work without extra cost: the same per-node process boundary
that makes uid/cgroup-based attribution possible is also exactly the boundary an external observer
(eBPF, cgroup accounting — the mechanism [warden](../warden) already validated) needs to watch a
node from *outside* its own trust domain, independent of whatever that node's own Strands telemetry
claims about itself. One piece of infrastructure earns both properties — attribution and unforgeable
ground truth — rather than needing to be built twice.

## Also surfaced, not resolved: an org/firm layer above the individual principal

Working mental model for what these agents are, in a professional-services context: provisioned
staff (paralegal-shaped), attributable to the individual principal (an attorney) and, by extension,
to their firm — held to a much higher reliability bar than casual automation. This maps onto a
chained-delegation model (subject → actor, scope narrowed per hop, the shape warrant already
implements at the token layer) with one layer missing: nothing here or in warrant currently models
a firm/org identity above the individual human principal. Worth resolving before this goes anywhere
near a real multi-person deployment.

## Not a fork, not a mega PR — checked against Strands' own precedent

Whether this should eventually be a large upstream contribution to `strands-agents/harness-sdk`
came up early, before any code exists. Checked rather than assumed: someone already filed nearly
this exact request — [#1010](https://github.com/strands-agents/harness-sdk/issues/1010), "Support
isolated AgentCore runtimes for individual agents in graph architectures" (per-agent isolation,
different IAM per agent, no shared memory space). A core Strands maintainer (`mkmeral`) closed it
as a duplicate with a working code sample: implement a custom `MultiAgentBase` node
(`RemoteAgentNode` in their example) that routes a node's execution to an isolated runtime via HTTP
instead of the default in-process `stream_async`. Their own words: *"this is how you can solve it
today."*

That's direct confirmation of this project's own architecture, not just a design choice made in
isolation: the maintainers' stance is "build this as a custom node using the extension point we
already ship," not "we'll add isolation machinery to core." Since siphonophore doesn't modify
Strands' own code at all — it's a new `MultiAgentBase` implementation, a dependency relationship,
not a diff against the SDK — there's structurally nothing to submit as a mega PR. Also checked:
[#2830](https://github.com/strands-agents/harness-sdk/issues/2830) (`BubblewrapSandbox`) and
[#2035](https://github.com/strands-agents/harness-sdk/issues/2035) (`sandlock`, kernel-level) were
both accepted as new *Sandbox backends* — new implementations of an existing interface get merged;
changes to core dispatch logic aren't the pattern that succeeds here, because the interface is
already the intended extension point.

What would be legitimate, scoped upstream contribution if it comes up while building: a specific
gap in the `MultiAgentBase`/hook contract (something `NodeResult` doesn't expose, a hook that fires
at the wrong point for identity provisioning to hook into cleanly) — a small, reviewable fix, not
"please adopt our orchestrator." Lower-stakes than even that: a GitHub Discussion describing the
use case and asking if anyone's approached the attribution/identity angle specifically, given the
team is demonstrably fast to respond and engaged with exactly this class of question.

## Status

Design only. Nothing built yet. Originated as a tangent inside `warrant`'s own STATUS.md
(2026-08-24), pulled out into its own repo because it's not a warrant-specific feature — it's
general Strands-harness infrastructure any Strands-based system would benefit from, warrant
included but not exclusively.
