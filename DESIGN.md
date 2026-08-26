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

## §8 — Platform attestation is a separate, lower layer than execution identity

Everything in §2 and §3 — a provisioned uid, a cgroup, a check-in protocol verified by the kernel —
only means anything if the kernel doing the verifying is itself trustworthy. Those mechanisms
establish *which process, on this host, did this* on a given occasion. They cannot establish
anything about the host itself — a compromised kernel can lie about uids, cgroup membership, and
`SO_PEERCRED` just as easily as a compromised agent can lie about what it did. This is a different
question, at a different granularity, and must not be conflated with per-execution identity.

Platform attestation answers it once, not per-intent: at broker startup, not at every Decision. A
hardware root of trust (a TPM quote) establishes independently verifiable evidence about the node's
identity and its measured state at the moment of the quote — signed PCR values, bound to a fresh
nonce so the evidence can't be replayed from an earlier boot. Stated precisely, because a quote is
easy to overclaim: it proves possession of a platform-bound key and the freshness and integrity of
the *specific measurements taken*, not that "the host hasn't been tampered with" in any general
sense. What those measurements actually cover — firmware and boot chain at minimum; the broker's own
binary and configuration only if something extends measurement that far (an IMA-style scheme, or
equivalent) — determines what the evidence is actually evidence *of*. Per-execution identity (§2)
and check-in (§3) then bind individual mediated executions to whatever platform context was
established this way; they are only as strong as what that evidence actually covers, not
automatically as strong as "the whole host is clean."

**Execution provenance is a distinct concern from platform attestation, sitting between it and §2's
runtime identity, and deserves to be treated as its own extension of §2 rather than folded into
either neighbor.** A provisioned uid and cgroup identify *which running process* is being observed;
they say nothing about whether that process is running the code the broker actually meant to
authorize. Where the Gate's dispatch selects an execution class (§2), it should also carry an
artifact identity — a content digest of the code/recipe being executed, not just an import path or
task-type label — so that what got authorized and what actually ran can be compared, the same
binding discipline §2 already requires for every other dispatch-relevant field.

The attestor that performs platform attestation is a customizable mechanism, not a hard requirement
baked into every conformant harness (§6) — TPM hardware is not universal, and a harness running
where none exists cannot attest what isn't there. What is required: when a harness does claim
platform attestation, it must be structurally impossible for a per-execution identity (§2) to be
granted, or a check-in (§3) to be trusted, without that host having already passed attestation for
the current boot — platform attestation is the base of the trust chain the rest of this design
stands on, never a parallel, optional check running alongside it.

**Left genuinely open, not resolved by this section: who attests the broker's own integrity.**
Platform attestation covers the node; §2's proposed execution provenance covers what the broker
chooses to run; neither covers whether the broker binary and configuration doing the choosing is
itself the expected one. That may collapse into platform measurement (if measurement is extended to
cover the broker, it becomes part of establishing the node's state, not a separate step) or it may
be a supply-chain/deployment-time concern (code signing, reproducible builds) outside the runtime
attestation chain entirely, resolved before the broker ever starts rather than by anything it checks
about itself at startup. Which of these it actually is has not been decided here.

## Explicitly open, not yet resolved

- The gate↔cognitive-loop protocol (MCP-native vs. something narrower) — §1 names it as a
  candidate, not a decision.
- Whether §3's four-valued reconciliation, once genuinely exercised across contradiction and
  unreported-activity cases (not just the corroborated case), needs a richer comparison than plain
  equality — e.g. partial matches, or claims that are ambiguous rather than cleanly true/false. A
  sharper version of the same question: reconciliation today compares exactly two booleans
  (claimed, observed), but a checked-in execution actually produces three independent signals —
  whether identity was established (check-in), whether a claim was made (self-report), and whether
  an effect was observed (ground truth). Collapsing "identity failed but an effect was still
  observed" into the same four-valued space as "identity held but the claim was false" may be
  losing a real distinction, not yet exercised because no current backend can produce that case
  (§2's `uid_cgroup_checkin` gates its own artifact code on check-in success, so an unattributed
  observed effect cannot currently occur through it — see the `uid_cgroup_checkin` bullet below for
  why that guarantee is specific to this one backend's wrapper, not architectural).
- An org/firm layer above an individual human Principal — delegation chains today only reach a
  single human principal, with no representation of an organization the principal belongs to.
- Whether "same process" should ever be a default execution class, or whether §2's policy should
  require an explicit, justified exception to stay in-process rather than treating it as a default.
- §8's platform attestation is undesigned below the level stated: no attestor implementation
  exists, and the exact mechanics (which TPM tooling, how a quote's PCR values map to "expected,"
  how a harness without TPM hardware degrades — refuses to start, or runs with attestation
  explicitly disclosed as absent rather than silently skipped) are all unresolved.
- §8's execution provenance is implemented and tested (`lab/008`, and combined with `uid_cgroup` in
  `lab/009`) as an inline-code digest. Real deployments would more likely authorize a *reference*
  (a module path, a container image digest, a package version) rather than inline source — what
  exactly gets hashed for a reference-based artifact, and how that generalizes past inline strings,
  remains open.
- Whether broker-integrity attestation is a platform-measurement concern or a supply-chain/
  deployment-time concern — §8 names the question without answering it.
- §3's check-in protocol and reconciliation are now wired into one execution backend
  (`uid_cgroup_checkin`, alongside the still-unchecked `uid_cgroup`) rather than staying
  freestanding primitives — but only there: a same_process or separate_process delegation has no
  check-in-gated or automatically-reconciled equivalent. The open question is sharper than "should
  check-in be the default for delegation": execution substrate (§2: same_process | separate_process
  | uid+cgroup | container | VM) and required assurance (unverified | process-identified |
  checked-in | reconciled | externally-observed, per §5) are plausibly two orthogonal axes that
  `uid_cgroup_checkin` currently conflates into one execution-class name. Naming an assurance level
  on `Decision` independently of execution class (rather than one execution-class string per
  substrate×assurance combination) would generalize cleanly, but is not justified yet by only two
  execution classes carrying any assurance variation — worth deferring until a third combination
  actually needs it, not building ahead of that pressure.
- `uid_cgroup_checkin`'s guarantee that a check-in failure cannot co-occur with an
  already-performed artifact effect is a property of its own wrapper (`_CHECKIN_CHILD_WRAPPER` gates
  `intent.artifact_code` on `perform_checkin()` succeeding first), not something the architecture
  enforces for every possible checked-in backend. A differently-shaped backend (e.g. one that
  starts the artifact concurrently with check-in for latency reasons) could produce a real,
  observed effect with a failed identity binding — `CheckinFailedError` already carries its
  `observations` for exactly this reason, but nothing today distinguishes "nothing happened" from
  "something happened, attribution is invalid" at the type level.
- A broker process that wants both the `uid_cgroup`/`uid_cgroup_checkin` tiers and the portable
  tiers available has to run entirely as root today. Three separate pieces, closed unevenly:
  `same_process`/`separate_process` refuse outright rather than silently inheriting root (raise
  unless a caller passes `allow_root=True` — closes the silent case). `useradd`/`userdel` now go
  through privilege separation for real — two self-validating wrapper scripts
  (`scripts/siphonophore-useradd`/`-userdel`), elevated via a scoped `sudo -n` only when not
  already root (`scripts/README.md`), validated on colima with a genuinely unprivileged user and a
  real sudoers grant, not just reasoned through. Cgroup management needs only delegation (`chown` a
  subtree once, no code change) — documented, not yet exercised end-to-end with an unprivileged
  broker. **Still fully open, but the interface is now pinned:** the `preexec_fn` privilege drop
  that spawns the artifact process under its target uid still requires the *forking* process to
  already be root — no unprivileged broker can perform that step itself yet.
  `contracts/spawn_helper.md` (PINNED) freezes the interface for the narrowly-privileged helper
  that will close this — an exact-argument-free `sudo` invocation, one multiplexed stdin stream
  crossing that boundary, genuinely separate fds past it — but no implementation exists yet.
  Nothing else should be built against that contract until it's been reviewed for any capability it
  might accidentally grant the broker. Until this closes, no broker can run the `uid_cgroup` tiers
  while staying unprivileged itself, regardless of the other two pieces being done. **What this
  helper explicitly does not, and structurally cannot, close: whether the broker's own request was
  ever authorized by `Gate.submit()` in the first place.** The helper's `SH-23` invariant provides
  execution-identity consistency and replay prevention (at most one spawn per `execution_id`), not
  an independent attestation of Gate authorization — the helper has no access to the Gate's own
  secret, and giving it one would expand its trusted surface rather than narrow it. This is the same
  gap this section already names: a broker whose own process is compromised already holds the
  Gate's secret and needs no help from `siphonophore-spawn` to mint a valid `Decision` for anything
  it wants. **Authorization belongs above the execution substrate** — closing this for real is a
  question of who attests the broker's own integrity, not something a spawn helper can be made to
  answer by construction.
