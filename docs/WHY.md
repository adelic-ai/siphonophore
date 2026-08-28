# Why Siphonophore exists

Siphonophore began with a question about multi-agent execution.

Agent SDKs such as Strands provide useful abstractions for composing and orchestrating multiple
agents. Those agents are logical identities inside an application — clearly distinguished at the
harness level — but that harness-level distinction is not the same thing as an independent execution
identity at the operating-system boundary.

In the Strands `Agent.as_tool()` path examined during this project, a parent and its sub-agents can
execute in-process under the same Unix process and UID. Strands can distinguish those agents at the
framework level while the operating system sees their machine effects as originating from the same
security principal. The logical identity doesn't disappear or become ill-defined — Strands still
knows exactly which agent is which. What's missing is something else: nothing outside the harness's
own trust domain can independently confirm that distinction.

Conceptually, the framework can distinguish:

    Agent A ─┐
             ├── agent runtime
    Agent B ─┘

while the operating system may see only:

    agent runtime ── uid 1000 ── machine effects

That distinction — harness-level logical identity versus independently attributable execution —
matters once the question changes from:

> Which agent does the framework say acted?

to:

> Which agent can be independently established to have caused the machine effect?

The framework can record that Agent B invoked a tool, delegated work, or produced a result — real,
meaningful bookkeeping — but that record originates inside the same application trust domain as the
agent it describes. An external observer may be able to establish that the shared runtime performed
effect X, without being able to independently establish that Agent B performed effect X specifically.
Attribution, confirmation, and attestation at agent granularity become difficult not because identity
is missing, but because nothing external exists to confirm an agent's own report against.

The same gap shows up one level up, too: establishing that a shared runtime executed something does
not by itself establish which logical agent caused it, whether that agent held legitimate authority,
or whether that authority was legitimately derived from whichever principal delegated the work.

Tracing, hooks, logs, or agent-specific instrumentation can be layered around a shared runtime to
close this gap after the fact — but those mechanisms then become part of the trust argument
themselves. They have to correctly distinguish identities the operating system doesn't distinguish,
stay complete across every effect-producing path, and resist interference or bypass by the very
runtime they're describing.

Siphonophore explores the opposite approach:

> **Preserve authority and establish execution identity before an effect occurs, so those properties
> do not have to be reconstructed afterward.**

## Historical progression

The architecture didn't arrive all at once, and it didn't arrive in the order its final shape might
suggest. Two mostly-separate threads of work converged into what `DESIGN.md` now describes.

**Thread one: what execution identity actually requires.** The project surfaced as a tangent inside a
sibling project's own status notes: Strands agent delegation shares one uid/pid across a parent and
every sub-agent it delegates to, with no OS-level way to attribute a sub-agent's actions independently
of its parent. The first design (`v1`, since removed from the repository) built a dedicated
orchestrator sitting alongside Strands, giving only sub-agents explicitly flagged for isolation
("severed" nodes) real OS identity — a dedicated uid, a cgroup, a nonce-based check-in protocol
verified through the kernel's `SO_PEERCRED`. It was built and validated for real, and it worked.

It was superseded anyway, for two reasons. First, Strands treats tool calls and delegation
asymmetrically — `Agent.as_tool()` unifies the *calling syntax* for both, but a sub-agent invoked
this way still just runs `stream_async()` in the parent's own process; the trust boundary was never
touched. A harness that mediates tool calls but not delegation reintroduces the same attribution gap
one layer up, regardless of how the delegation-specific piece is isolated. Second, giving every
"severed" sub-agent its own OS identity was itself the wrong granularity: most agents in a real
deployment should stay part of a shared process, and blanket per-agent OS identity over-corrects.
**What actually needs isolation turned out to be a function of authority width, lifetime, and
untrusted-input exposure for a specific action — not a fixed property of being a sub-agent at all.**
Distinct UIDs, in other words, are generally unnecessary for every agent or sub-agent; they're one
available execution mechanism among several, selected per action rather than assigned per agent.

That reframing is also what generalized the problem past Strands specifically. Once isolation
strength is a per-action policy decision rather than a fixed per-agent assignment, the same
attribution gap — and the same fix — applies to any harness where a security-relevant effect can be
produced without an independently checkable execution identity behind it. That's the frame this
project later used to examine NVIDIA NeMo Fabric's bundled adapters (see the sibling `Palpon`
project) and OpenClaw's own orchestration model, neither of which Siphonophore integrates with today.

**Thread two: what delegated authority actually requires.** Separately — and starting from work
already in place, not from the uid/cgroup thread above — the project built one mediation gate that
every effect-producing intent passes through (`Intent → Gate → Decision → Executor`), proven first as
a minimal, portable pipeline. Delegation was initially modeled as *just another Intent kind*, routed
through the identical gate a tool call uses. That demonstrated repeated mediation — the same gate
handles both — but it did not demonstrate delegated authority *provenance*: nothing established that
a delegate's authority actually derived from its delegator, was bounded by what the delegator itself
held, or traced to a real originating grant.

This was a category error, not a missing feature, and it was caught later by a dedicated maturity
assessment that traced the actual code against the architecture's own stated central claim rather
than trusting design docs or comments. The fix was `Order`/`Authority`/`Scope` (`DESIGN.md` §9): an
`Intent` is an attempted *exercise* of authority, never its *source*; delegation is a distinct
operation — `Gate.delegate()` — that mints a new, independently-scoped `Authority` from a verified
parent, never from an Intent. This correction is unrelated to the execution-identity generalization
above; it fixed a gap in the mediation model itself, discovered independently and later, once real
code existed to assess against.

**Where the two threads meet.** The current architecture treats these as genuinely separate concerns
that happen to compose: `Order`/`Authority`/`Scope` establishes and attenuates *what* may be done and
by whom, independent of *how* it must execute; execution requirements (thread one's generalization)
determine the isolation and identity a specific authorized action needs, independent of how much
authority granted it. Neither dimension is a proxy for the other — an early draft that used execution
class as a delegation ceiling had to be corrected for exactly this reason (`DESIGN.md` §2). The result
is the architecture `README.md` summarizes: preserving delegated authority, authorization, execution
requirements, and independent evidence across the boundary where agent reasoning becomes machine
effect.

Full detail on both threads, including real bugs found and fixed along the way, lives in
`HISTORY.md`.
