# siphonophore

**Mediated, attributable execution for agent systems.**

Siphonophore is an experimental execution-security SDK, with a reference harness demonstrating how
delegated authority, execution requirements, and machine effects can remain independently checkable
across agent execution boundaries.

It separates the authority to perform an action from the execution requirements under which that
action may run, and cryptographically binds both into the decision consumed by the execution layer.

## Why this exists

Siphonophore began with a question about multi-agent execution. Agent SDKs such as Strands and
OpenClaw let a parent agent delegate to sub-agents that are clearly distinguished at the harness
level — but in the `Agent.as_tool()` path examined during this project, those sub-agents can execute
in-process, under the same Unix process and UID as their parent. The logical identity the harness tracks doesn't
disappear or become ill-defined; it stays perfectly well-formed inside the application. What's
missing is something else: an operating system observing that process sees one shared execution
identity, not the harness's own distinctions, so nothing outside the harness's own trust domain can
independently confirm which agent actually caused a given machine effect.

That distinction — **harness-level logical identity versus independently attributable execution** —
is the whole of what this project is about: whether the authority behind a security-relevant effect
can remain independently checkable once that effect becomes real execution, or whether attribution
has to be reconstructed afterward, from evidence produced inside the same trust domain that produced
the effect. Siphonophore explores the opposite approach: **preserve authority and establish
execution identity before an effect occurs**, so those properties don't have to be reconstructed
after the fact.

Full argument, and the historical progression from a Strands-specific fix to this general
architecture: **[`docs/WHY.md`](docs/WHY.md)**.

## Current state — August 2026

**Siphonophore demonstrates its core authority-to-execution properties composing together in one
real, Linux-backed test path** — not merely as separately-validated primitives, and not as a single
uniform, universally-independent guarantee:

    delegated bounded Authority
        → Broker.dispatch()
        → Gate
        → Decision
        → Executor
        → real OS-backed execution
        → evidence, reconciled where invoked

Each arrow is a distinct relation, established by a different mechanism: cryptographic
re-verification for authority and decision binding, a kernel-established fact for execution identity
specifically where check-in is used, and a policy-neutral comparison for reconciliation. All of it
holds conditional on the integrity of the one broker process holding the Gate's signing secret — see
`DESIGN.md` §9 for exactly what the delegation chain's provenance fields do and do not prove, and the
"Trust boundaries" section below for the broker's role as the shared trust root every relation here
is ultimately conditional on.

The current Linux implementation instantiates "real OS-backed execution" as a genuine ephemeral UID
and cgroup v2 leaf per execution — see the "Real OS-level execution identity" bullet below for which
execution class establishes that identity independently of the executing process, and which does
not. "Evidence, reconciled where invoked" means the Belnap-logic comparison of self-report against
independently-collected ground truth runs only when a caller supplies `outdir`; it is not a property
of every dispatch. **A distinct UID is an execution mechanism, not the identity model** — the
architecture doesn't require, and current policy doesn't grant, a separate OS identity to every agent
or sub-agent. See [`docs/EXECUTION.md`](docs/EXECUTION.md) for why execution controls are a set of
independent dimensions rather than one fixed ladder every agent climbs.

The negatives are part of this claim, not an appendix: out-of-scope delegated authority is refused,
artifact substitution is refused before the privileged execution boundary ever runs, and a genuinely
authentic execution identity lying about what it did does not become corroborated merely because its
identity is real.

The reference harness is not yet a complete multi-agent, multi-model harness. Two independently
running `CognitiveLoop` instances, sharing one `Gate`/`Broker`, compose correctly — one holding its
own root `Authority`, the other holding an `Authority` delegated from the first, each producing its
own model-generated intent. What doesn't exist yet is an *orchestration* component: something that
decides when to delegate, constructs the second loop, and supplies its own model, in a live
deployment rather than a test.
That's harness/product capability, not a missing piece of the security architecture — the thesis is
demonstrated without it.

Today, Siphonophore demonstrates:

- **Core mediation** — `Intent → Gate → Decision → Executor → Effect`. Security-bearing,
  dispatch-relevant fields — intent kind, execution class, artifact digest, authority/order
  provenance — are cryptographically bound into the `Decision`. `Executor` independently re-verifies
  the `Decision` and artifact rather than trusting that an upstream component already checked them.

- **Delegated authority** — `Order → Authority → delegated Authority`. One principal can derive
  constrained authority for another; `Gate` independently verifies the parent at each derivation,
  enforces scope attenuation and delegation depth, and preserves the originating `order_id`.
  Exercised through the ordinary public `Broker.dispatch(intent, authority=...)` interface — no
  caller needs to know `Gate`/`Executor` exist. **An `Authority` itself has no expiry, revocation, or
  consumption semantics** — a reusable bearer capability for as long as its scope stays meaningful,
  distinct from the replay protections `Decision` and `siphonophore-spawn`'s `SH-23` provide for
  other objects. A leaked delegated `Authority` remains exploitable indefinitely within its scope;
  narrowing this is orchestration-layer design work, not yet built. Full model: `DESIGN.md` §9.

- **Real OS-level execution identity** — the `uid_cgroup` and `uid_cgroup_checkin` backends both
  provision a genuine ephemeral system user and real cgroup v2 leaf per execution. **Only
  `uid_cgroup_checkin` independently establishes that identity through the kernel** (`SO_PEERCRED`) —
  a live check-in the executing process cannot forge or assert its way past. Plain `uid_cgroup`
  provisions the identical real UID/cgroup but reads its own identity from `/proc`, in the same
  process asserting the rest of the chain — a genuine kernel fact, but not independently
  cross-checked by anything else.

- **Execution requirements bound to authorization** — policy currently selects among `same_process`,
  `separate_process`, `uid_cgroup`, and `uid_cgroup_checkin`. The selected execution class is
  cryptographically bound into the resulting `Decision`, preventing an authorized execution
  requirement from being silently substituted after authorization. Full model:
  [`docs/EXECUTION.md`](docs/EXECUTION.md).

- **Negative enforcement** — tests exercise fabricated and spliced authority, scope expansion,
  principal impersonation, artifact substitution, forged or modified Decisions, replay attempts, and
  execution-class tampering. These cases are refused rather than merely logged.

- **`siphonophore-spawn`** — a minimal, dependency-free C helper for crossing the narrow privileged
  boundary UID/cgroup execution requires, without running the entire broker as root. Wired into the
  normal `Executor` dispatch path (`SpawnHelperBackend`); confirmed running under a real unprivileged
  system user, not just as root. **What the helper establishes: execution-identity consistency and
  replay prevention** — at most one real spawn per `execution_id`. **What it does not, and
  structurally cannot, establish: that the `execution_id` it was asked to spawn corresponds to a
  Decision `Gate.submit()` actually minted.** That check runs one layer up, inside the broker, before
  the helper is ever invoked — sound as long as the broker process itself has not been compromised.
  See `contracts/spawn_helper.md`'s `SH-23` section for the full trust-boundary statement. A finished
  execution's cgroup leaf is not automatically removed — a disclosed, deliberate limitation (see
  `DESIGN.md`), not an oversight.

- **Independent evidence and reconciliation** — execution check-in and OS observations are a channel
  distinct from agent self-report. `audit.py` uses
  [Belnap four-valued logic](https://en.wikipedia.org/wiki/Four-valued_logic) to keep `corroborated`,
  `contradiction`, `unreported_activity`, and `no_evidence` distinct rather than collapsing them into
  a single match/no-match boolean:

  | Agent claim | OS observation | State |
  |---|---|---|
  | yes | yes | corroborated |
  | yes | no | contradiction |
  | no | yes | unreported activity |
  | no | no | no evidence |

  Composed with delegation and the unprivileged-broker path via `CheckedInSpawnHelperBackend`, with
  no changes needed to `siphonophore-spawn.c` or the pinned spawn-helper contract. Full treatment,
  including the execution-identity-versus-logical-agent-identity distinction:
  [`docs/EVIDENCE.md`](docs/EVIDENCE.md).

One real test (`tests/test_harness_loop_linux.py`) demonstrates this full composition in a single
execution, including a negative case: a delegate whose real check-in verifies but whose
self-report lies about what it did reconciles as `contradiction`/`unreported_activity`, never
`corroborated` — a genuine identity plus a false claim is still refused as confirmation. A separate
test in the same file drives the identical composition with two real, independently running
`CognitiveLoop` instances instead of a single test actor.

### Not yet implemented or integrated

- `CognitiveLoop` can hold and exercise a delegated `Authority`, and two independently running
  instances are proven to compose correctly. There is no *orchestration* layer yet — nothing decides
  when to delegate, spins up a second agent, or picks its model in a live deployment; today that's
  done by hand (test code, or `examples/repl.py` if extended).
- Container and VM execution substrates are not implemented.
- Platform attestation and production credential delivery are not implemented — including any
  execution-specific identity mechanism (SPIFFE/SPIRE, JWT+Vault, or otherwise). See
  [`docs/EXECUTION.md`](docs/EXECUTION.md) for the architectural direction, not a built mechanism.
- `Scope` currently constrains intent kinds and delegation depth. Resource- and payload-level
  constraints are deliberately deferred.
- Multi-model support currently exists at the model-interface level; orchestration of multiple live
  model providers is not yet implemented.

`DESIGN.md` contains the complete current architecture, guarantees, trust assumptions, and open
questions. The claims above describe what is implemented today, not everything the architecture may
eventually support.

## Architecture

Siphonophore treats two properties as deliberately independent:

    AUTHORITY                         EXECUTION
    ---------                         ---------
    What may be done?                 How must it execute?
    Who holds that authority?         What isolation is required?
    Where did it derive from?         What execution identity is required?
    What may be delegated?            What substrate satisfies the requirement?

A child agent requiring stronger isolation than its parent has not received greater authority — it
may simply be performing work with a different risk profile. Execution requirements follow the
specific authorized action; an agent doesn't need to permanently "live in a VM" or under one fixed
isolation tier.

**Authority and delegation.** An `Intent` is an attempted exercise of authority, never its source.
`Order` is the ungrounded root of a delegation chain; `Authority` is a standing, principal-scoped
capability derived either from a verified `Order` or from a verified parent `Authority`; `Scope`
constrains what an `Authority` permits and how far it may be further delegated. `Gate` independently
re-verifies the parent at every derivation — it never trusts that a caller already checked one — and
`Gate.submit()` re-verifies the supplied `Authority` again at the point it's exercised, before minting
a `Decision`. Full model, including the precise (and deliberately narrow) guarantee the delegation
chain's provenance fields actually carry: `DESIGN.md` §9.

**Execution model.** Execution requirements are a set of independent dimensions — process/PID
lineage, cgroup, UID/GID, sandbox or namespace, container or VM, credentials, filesystem policy,
network policy, resource limits — not a single weakest-to-strongest ladder every agent climbs. The
dimension(s) a given effect needs are a policy decision proportional to that effect's actual risk,
bound into the same `Decision` as the authorization so an authorized execution requirement can't be
silently substituted. Today's Linux implementation exercises exactly two of these dimensions for real
— UID/GID and cgroup — via `uid_cgroup`/`uid_cgroup_checkin`; the rest are architectural direction,
not built. Full per-dimension treatment: [`docs/EXECUTION.md`](docs/EXECUTION.md).

**Trust boundaries.** The central rule: whenever something accepted as safe is consumed downstream
with greater authority, that handoff is named explicitly and made independently checkable where
practical, rather than trusted because an upstream component already checked it once.[^trusted-enough]
That's why
`Executor` re-verifies the `Decision` it's handed instead of trusting `Gate` checked it already, and
why execution check-in obtains the peer identity from the kernel instead of accepting one the spawned
process asserts about itself. Full treatment: `DESIGN.md` §4.

## Relationship to other agent systems

Siphonophore is not an agent-development SDK and doesn't compete with one. Systems like Strands and
OpenClaw can supply cognition, logical agent identity, models, tool ecosystems, and orchestration —
real, substantial engineering problems Siphonophore doesn't attempt to solve. Siphonophore is an
execution-security SDK concerned with a narrower boundary: whether the authority behind a
security-relevant effect stays independently checkable at the moment that effect becomes real
execution, regardless of which harness produced the intent to perform it.

Siphonophore does not currently integrate with Strands, OpenClaw, or any other agent harness — both
are referenced here only as examples of the class of system this project could sit beneath. Doing so
meaningfully requires a harness whose security-relevant effects pass through a boundary Siphonophore
can actually force every such effect through, without a path that bypasses it — not every harness
necessarily provides one.

## Repository shape

- **`siphonophore_core/`** — `Intent` / `Effect`, `Order` / `Authority` / `Scope`,
  `Policy` / `Decision` / `Gate`, `Executor` and execution-class backends, execution identity,
  check-in, and audit/reconciliation.
- **`spawn_helper/`** — `siphonophore-spawn`, the minimal C privileged helper for crossing the
  narrow privilege boundary required by UID/cgroup execution. Its pinned interface lives in
  `contracts/spawn_helper.md`.
- **`scripts/`** — privilege-separated account-management wrappers and sudoers templates.
- **`siphonophore_harness/`** — the current minimal cognitive loop, model interface,
  Anthropic-backed model implementation, intent parsing, and broker.
- **`examples/repl.py`** — interactive live-model reference harness.

## Requirements

- Python ≥ 3.10
- Real root on real Linux with cgroup v2 for the `uid_cgroup` / `uid_cgroup_checkin` execution
  tiers and their tests. Everything else is portable.
- A C11-capable `cc` only when building `spawn_helper/siphonophore-spawn`.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

For the real-model reference harness:

```bash
.venv/bin/pip install -e ".[anthropic]"
export ANTHROPIC_API_KEY=sk-...
```

## Running the tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -v
```

Portable tests require no special privileges.

Tests marked `linux_root_only` require real root on real Linux with cgroup v2. They exercise real
system-user provisioning, cgroups, privilege drops, concurrent Unix-socket check-ins, kernel
identity verification, and the privileged C helper rather than mocked equivalents.

## Running the live harness

```bash
.venv/bin/python examples/repl.py --model <a current Anthropic model id>
```

Each turn drives a real model call through intent parsing, `Gate`, and `Executor`.

The current live harness uses the authority-less path — a single `CognitiveLoop`/`Broker` pair, one
principal, no delegation. `CognitiveLoop` and `Broker.dispatch()` are both authority-aware now (see
**Current state** above) and a second, real live agent could be constructed the same way
`tests/test_harness_loop_linux.py` does — but `examples/repl.py` itself doesn't do that yet; nothing
here decides when to spin one up or supplies its model. That's still separate, later work.

## Documentation

- **[`docs/WHY.md`](docs/WHY.md)** — the full argument for why Siphonophore exists, and the
  historical progression from a Strands-specific fix to this general architecture.
- **[`docs/EXECUTION.md`](docs/EXECUTION.md)** — execution requirements as independent dimensions,
  what's implemented today versus architectural direction.
- **[`docs/EVIDENCE.md`](docs/EVIDENCE.md)** — independent evidence and Belnap reconciliation in
  full, including the execution-identity-versus-logical-agent-identity distinction.
- **`DESIGN.md`** — current architecture, guarantees, trust boundaries, assumptions, and explicitly
  open questions.
- **`HISTORY.md`** — experiments, failures, corrections, and the reasoning by which the current
  architecture was reached.
- **`contracts/`** — pinned contracts for narrow security-critical boundaries whose implementations
  remain subordinate to the contract.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).

[^trusted-enough]: Adapted from Elad Meged, *Trusted Enough to Run*, Black Hat USA 2026:
    <https://blackhat.com/us-26/briefings/schedule/?#trusted-enough-to-run-breaking-ai-agents-in-official-workflows-53406>.
    Also published by Meged (founding engineer, Novee) as "Critical Flaws in Anthropic, Google,
    and OpenAI's Coding Agents":
    <https://novee.security/blog/critical-flaws-in-anthropic-google-and-openais-coding-agents/>.
