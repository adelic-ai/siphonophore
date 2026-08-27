# siphonophore

**Mediated, attributable execution for agent systems.**

Siphonophore is an experimental SDK and reference harness for making authority, execution identity,
and machine effects independently checkable across agent execution boundaries.

It separates the authority to perform an action from the execution requirements under which that
action may run, and cryptographically binds both into the decision consumed by the execution layer.

## Current state — August 2026

Siphonophore is under active development. Its core authority-to-execution path is implemented and
demonstrated end-to-end against real Linux OS boundaries. It is not yet a complete multi-agent,
multi-model harness.

Today, Siphonophore demonstrates:

- **Core mediation** — `Intent → Gate → Decision → Executor → Effect`. Security-bearing,
  dispatch-relevant fields including intent kind, execution class, artifact digest, and
  authority/order provenance are cryptographically bound into the `Decision`. `Executor`
  independently re-verifies the `Decision` and artifact rather than trusting that an upstream
  component already checked them.

- **Delegated authority** — `Order → Authority → delegated Authority`. One principal can derive
  constrained authority for another. At each derivation, `Gate` independently verifies the parent,
  enforces scope attenuation and delegation depth, and preserves the originating `order_id`.
  Exercise of delegated authority is independently re-verified before a `Decision` is minted.

- **Real OS-level execution identity** — the `uid_cgroup` / `uid_cgroup_checkin` backends provision
  a genuine ephemeral system user and real cgroup v2 leaf per execution. Check-in independently
  establishes the spawned process's identity through the kernel (`SO_PEERCRED`) rather than
  accepting an identity asserted by the process itself.

- **Execution requirements bound to authorization** — policy can currently select among
  `same_process`, `separate_process`, `uid_cgroup`, and `uid_cgroup_checkin`. The selected
  execution class is cryptographically bound into the resulting `Decision`, preventing an
  authorized execution requirement from being silently substituted after authorization.

- **Negative enforcement** — tests exercise fabricated and spliced authority, scope expansion,
  principal impersonation, artifact substitution, forged or modified Decisions, replay attempts,
  and execution-class tampering. These cases are refused rather than merely logged.

- **`siphonophore-spawn`** — a minimal, dependency-free C helper for crossing the narrow privileged
  boundary required by UID/cgroup execution without requiring the entire broker to run as root.
  The helper is implemented and independently validated against a real Linux privilege boundary.

- **Independent evidence and reconciliation** — execution check-in and OS observations provide a
  channel distinct from agent self-report. `audit.py` uses
  [Belnap four-valued logic](https://en.wikipedia.org/wiki/Four-valued_logic) to preserve
  distinctions between corroborated claims, unsupported claims, observed-but-unreported activity,
  and absence of evidence.

The complete demonstrated authority path is:

    Order
      │
      ▼
    Authority A
      │
      │ Gate.delegate()
      ▼
    Authority B
      │
      │ exercised by B
      ▼
    Intent
      │
      ▼
    Gate
      │
      ▼
    Decision
      │
      ▼
    Executor
      │
      ▼
    real UID / cgroup boundary
      │
      ▼
    Effect

(Check-in and Belnap reconciliation are real and independently tested, but against the
`uid_cgroup_checkin` backend directly — not yet composed into the same test as the delegation path
above. See "Not yet implemented or integrated" below.)

### Not yet implemented or integrated

- `Broker` / `CognitiveLoop` do not yet expose authority-aware dispatch. Exercising delegated
  `Authority` currently means calling `Gate.submit(intent, authority=...)` directly.
- There is not yet a second independently running, model-driven agent loop exercising delegated
  authority. Delegation is real at the authority/Gate/Executor layer but is not yet live
  multi-agent orchestration.
- `siphonophore-spawn` is independently validated but is not yet connected to the normal
  `Executor` dispatch path.
- Container and VM execution substrates are not implemented.
- Platform attestation and production credential delivery are not implemented — including any
  execution-specific identity mechanism (SPIFFE/SPIRE, JWT+Vault, or otherwise); see "Credential
  and identity delivery" below for the architectural direction, not a built mechanism.
- `Scope` currently constrains intent kinds and delegation depth. Resource- and payload-level
  constraints are deliberately deferred.
- Multi-model support currently exists at the model-interface level; orchestration of multiple
  live model providers is not yet implemented.

`DESIGN.md` contains the complete current architecture, guarantees, trust assumptions, and open
questions. The claims above describe what is implemented today, not everything the architecture
may eventually support.

## Why this exists

Siphonophore began with a simple question about multi-agent execution.

Agent SDKs such as Strands provide useful abstractions for composing and orchestrating multiple
agents. Those agents are logical identities inside an application, however, and are not necessarily
independent execution identities at the operating-system boundary.

In the Strands `Agent.as_tool()` path examined during this project, a parent and its sub-agents can
execute in-process under the same Unix process and UID. Strands can distinguish those agents at the
framework level while the operating system sees their machine effects as originating from the same
security principal.

Conceptually, the framework can distinguish:

    Agent A ─┐
             ├── agent runtime
    Agent B ─┘

while the operating system may see only:

    agent runtime ── uid 1000 ── machine effects

That distinction matters when the question changes from:

> Which agent does the framework say acted?

to:

> Which agent can be independently established to have caused the machine effect?

Once multiple logical agents collapse into the same machine identity, independently determining
what **one particular agent** actually did becomes difficult. The framework can record that Agent B
invoked a tool, delegated work, or produced a result, but that evidence originates inside the same
application trust domain.

An external observer may therefore be able to establish that:

    the shared runtime performed effect X

without being able to independently establish that:

    Agent B performed effect X

That makes strong agent-level **attribution, confirmation, and attestation** difficult. An agent can
report what it did, but without an independently grounded execution identity there may be nothing
external against which to confirm that claim at agent granularity.

Likewise, establishing that a shared runtime executed something does not by itself establish which
logical agent caused it, whether that agent possessed legitimate authority, or whether that
authority was legitimately derived from the principal that delegated the work.

Tracing, hooks, logs, or agent-specific instrumentation can be added around a shared runtime, but
those mechanisms then become part of the trust argument themselves. They must correctly distinguish
identities that the operating system does not distinguish, remain complete across every
effect-producing path, and resist interference or bypass by the runtime they are intended to
describe.

The security property is being reconstructed after the identities have already collapsed.

Siphonophore explores the opposite approach:

> **Preserve authority and establish execution identity before an effect occurs, so those
> properties do not have to be reconstructed afterward.**

## Architecture

Siphonophore treats two properties as deliberately independent:

    AUTHORITY                         EXECUTION
    ---------                         ---------
    What may be done?                 How must it execute?
    Who holds that authority?         What isolation is required?
    Where did it derive from?         What execution identity is required?
    What may be delegated?            What substrate satisfies the requirement?

The distinction matters.

A child agent requiring stronger isolation than its parent has not received greater authority; it
may simply be performing work with a different risk profile.

Likewise, an agent does not need to permanently "live in a VM," "live in a container," or execute
under one fixed isolation tier. Execution requirements can follow the particular authorized action.

The architecture therefore looks roughly like:

    Order
      │
      ▼
    Authority A
      │
      │ Gate.delegate()
      ▼
    Authority B
    (attenuated Scope)
      │
      │ exercised by
      ▼
    Intent
      │
      ▼
    Gate
      │
      ├── verify authority
      ├── evaluate policy
      └── select execution requirement
      │
      ▼
    Decision
      │
      ├── authority provenance
      ├── authorized effect
      ├── artifact identity
      └── execution requirement
      │
      ▼
    Executor
      │
      ├── independently verify Decision
      ├── independently verify artifact
      └── dispatch only to authorized substrate
      │
      ▼
    Execution substrate
      │
      ▼
    Effect + independent evidence

### Granting authority

`issue_order`, `grant_root_authority`, and `delegate` establish and derive authority.

Delegation is not an `Intent` kind.

`Gate` independently verifies parent authority before deriving child `Authority`, constrains the
resulting `Scope`, enforces delegation depth, and carries the originating `order_id` forward.

The child `Authority` is a Gate attestation to the derivation checks performed when it was minted.
It does not independently reconstruct its entire ancestry.

### Exercising authority

An `Intent` represents a principal attempting to exercise authority it holds.

`Gate.submit()` independently verifies the supplied `Authority`, confirms that it belongs to the
principal exercising it, checks the requested operation against its `Scope`, evaluates policy, and
mints a cryptographically bound `Decision`.

`Executor` then independently verifies that `Decision` before allowing an execution backend to
produce the effect.

An `Intent` is therefore an **exercise of authority, never its source**.

This distinction was learned experimentally. An earlier design treated delegation as another
effect-producing `Intent` and attempted to prove delegation by sending it through the same
mediation primitive as a tool call.

That demonstrated repeated mediation, but it did not establish that the delegate actually
possessed authority derived from the delegator.

`Order` / `Authority` / `Scope` replace that model.

### Execution requirements

Authority and execution isolation remain orthogonal:

    Intent
      │
      ├── Authority / Scope ──► may this happen?
      │
      └── consequence/policy ─► how must it execute?
                                      │
                                      ▼
                               execution class

The selected execution class is bound into the same `Decision` as the authorization. A caller
therefore cannot obtain authorization for one execution requirement and silently dispatch the
effect through another.

Today, Siphonophore implements:

    same_process
         │
    separate_process
         │
      uid_cgroup
         │
    uid_cgroup_checkin

The architecture deliberately does not make those particular substrates part of the authority
model.

Future execution backends may include containers, VMs, or other substrates without changing what
`Order`, `Authority`, `Intent`, or `Decision` mean.

That leaves room for execution requirements to scale with the particular work being performed:

    narrow / low-consequence work
                │
                ▼
             process

    stronger execution identity
                │
                ▼
           UID / cgroup

    stronger containment
                │
                ▼
            container
             (future)

    stronger isolation
                │
                ▼
               VM
             (future)

These are illustrative policy choices, not fixed Siphonophore mappings.

The architectural property is that the execution requirement belongs to the authorized action and
can be independently enforced at the execution boundary.

This also leaves the architecture open to heterogeneous agent systems: different agents may be
backed by different models, and different actions by those agents may require different execution
substrates. Those capabilities are not all implemented today; the separation in the architecture
is intended to avoid making them require a redesign later.

### Credential and identity delivery

Execution requirements determine how an effect must execute. A related, deliberately separate
question is what machine identity or credentials that particular authorized execution needs to act
on anything beyond the local host — an API call, a cloud resource, a downstream service.

    Authority
        │
        ▼
      Intent
        │
        ▼
       Gate
        │
        ▼
     Decision
        │
        ├── execution requirement
        └── credential / identity requirement
                    │
                    ▼
             execution substrate
                    │
                    ▼
        execution-specific credentials

This is architectural direction, not a built mechanism — nothing below is implemented today. The
intended property is that credentials follow the specific authorized execution rather than being
ambient credentials every agent in a shared harness inherits merely by running inside it. A
narrowly scoped, structured workload might eventually receive a SPIFFE/SPIRE-issued workload
identity; a more free-form agent workload might instead need short-lived credentials issued as a
JWT through Vault. Neither technology is committed to here — the specific mechanism matters far
less than the property: credential scope should be bound to what a specific execution was actually
authorized to do, not inherited from whatever process happens to be running the agent.

This keeps the three questions this design treats separately genuinely separate, rather than
letting a fourth concern quietly attach itself to one of the existing three:

- **Authority scope** — what the principal may do.
- **Execution requirement** — how the effect must execute.
- **Credential/identity delivery** — what machine identity or narrowly scoped credentials that
  particular authorized execution needs.

### Trust boundaries

The central rule is:

> **Whenever something accepted as safe is consumed downstream with greater authority, identify
> that handoff explicitly and make the security-bearing property independently checkable where
> practical.**[^trusted-enough]

That is why `Executor` does not merely trust that `Gate` previously verified a request.

It verifies the `Decision` again.

That is why artifact identity is not merely recorded when authorization occurs.

It is recomputed before execution.

That is why execution check-in does not accept a principal identity reported by the spawned
process.

It obtains the peer identity from the kernel.

And that is why delegated authority is not accepted merely because a caller supplies an authority
identifier.

`Gate` verifies the actual parent `Authority` object before deriving another one.

The objective is not to eliminate trust. It is to make important trust boundaries explicit and to
avoid relying on an upstream assertion where an independent check can be made at the point that
assertion becomes security-critical.

### Independent evidence

Agent self-report and machine observation are intentionally separate evidence channels.

An agent may claim:

    "I performed effect X"

while an independent execution boundary may observe:

    principal B performed machine effect X

Neither observation is silently substituted for the other.

Siphonophore's audit layer uses Belnap four-valued logic to preserve the distinction:

| Agent claim | OS observation | State |
|---|---|---|
| yes | yes | corroborated |
| yes | no | unsupported claim |
| no | yes | unreported effect |
| no | no | no evidence |

The purpose is not to make the agent transcript irrelevant. It is to avoid treating the agent's
description of its own behavior as independent evidence of that behavior.

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

The current live harness uses the authority-less path. Authority-aware multi-agent orchestration is
not yet exposed through `Broker` / `CognitiveLoop`; see **Current state** above.

## Documentation

- **`DESIGN.md`** — current architecture, guarantees, trust boundaries, assumptions, and explicitly
  open questions.
- **`HISTORY.md`** — experiments, failures, corrections, and the reasoning by which the current
  architecture was reached.
- **`contracts/`** — pinned contracts for narrow security-critical boundaries whose implementations
  remain subordinate to the contract.

## License

Apache 2.0 — see `LICENSE`.

[^trusted-enough]: Adapted from Elad Meged, *Trusted Enough to Run*, Black Hat USA 2026:
    <https://blackhat.com/us-26/briefings/schedule/?#trusted-enough-to-run-breaking-ai-agents-in-official-workflows-53406>.
    Also published by Meged (founding engineer, Novee) as "Critical Flaws in Anthropic, Google,
    and OpenAI's Coding Agents":
    <https://novee.security/blog/critical-flaws-in-anthropic-google-and-openais-coding-agents/>.
