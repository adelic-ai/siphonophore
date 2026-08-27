# siphonophore

SDK for mediated, attributable agent execution — every effect-producing action, and every
derivation of delegated authority, passes through one cryptographically-bound `Gate`, with real
OS-level execution identity and independent, kernel-verified ground truth.

## Why this exists

Existing agent SDKs (Strands and others) unify how a tool call and a sub-agent delegation are
*invoked*, but not how they're *authorized* — a sub-agent still runs in-process, sharing the
parent's uid/pid, with no independent way to attribute what it actually did versus what it claims
to have done. That gap matters more as agentic systems take on higher-stakes, less-supervised work
(CI/CD automation, delegated sub-tasks, unattended runs) — exactly the direction security
expectations for these systems are heading. siphonophore is not a framework for building agents
faster; it's one that makes an agent's actions structurally impossible to leave unattributed.

## Current state

Real and validated — not just written, exercised for real including on a genuine root Linux host,
not against fakes:

- **Core mediation** — `Intent → Gate → Decision → Executor → Effect`. Every dispatch-relevant
  field (kind, execution class, artifact digest, and now authority/order provenance) is
  cryptographically bound and independently re-verified at each step, never trusted because an
  upstream check already accepted it.
- **Delegated authority** — `Order`/`Authority`/`Scope` (`siphonophore_core/authority.py`): one
  principal deriving constrained authority for another, checked against a real originating grant,
  never exceeding what the parent held, independently re-verified at the point it's exercised.
  Demonstrated end-to-end through a real OS-backed execution tier, not only as isolated unit tests.
- **Real OS-level execution identity** — the `uid_cgroup`/`uid_cgroup_checkin` backends provision a
  genuine, ephemeral system user and a real cgroup v2 leaf per execution; check-in independently
  verifies a spawned process's identity via the kernel (`SO_PEERCRED`), never anything the process
  asserts about itself.
- **`siphonophore-spawn`** (`spawn_helper/`) — a minimal, dependency-free C helper that lets an
  unprivileged broker use the `uid_cgroup` tiers without running as root itself. Built and
  independently validated (16 real tests on a genuine Linux host). **Not yet wired into the
  `Executor` dispatch path** — see below.
- **Belnap reconciliation** (`audit.py`) — self-report vs. independently-observed ground truth,
  compared with four-valued logic rather than a fuzzy boolean match.

Honestly not done yet:

- `Broker`/`CognitiveLoop` — the live, model-driven harness — don't expose authority-aware
  dispatch. Exercising a delegated `Authority` today means calling `Gate.submit(intent,
  authority=...)` directly; it's demonstrated in tests, not yet reachable from `examples/repl.py`.
- No real second, independently-running agent loop exists yet — delegation is proven at the
  `Gate`/`Executor` level, not yet orchestrated between two live model-driven agents.
- `siphonophore-spawn` is a standalone, validated mechanism; no `ExecutionBackend` calls it yet.
- `container`/`VM` execution tiers, platform attestation, real credential delivery, and multi-model
  support beyond `Model` being swappable in principle — none of these exist.

`DESIGN.md`'s "Explicitly open" section names every one of these, and more, precisely — read it
before assuming a guarantee holds that isn't actually enforced yet.

**Where this is headed:** a small, durable authority-mediation and execution-identity layer that
something else can sit on top of or embed — not a general-purpose agent framework competing with
existing agent SDKs on features. The scope stays deliberately narrow: the security decision between
an agent's intent and its execution, not the runtime around it.

## Architecture

```
Order → Authority ──┐
       (delegate)   │
                     ▼
Principal → Intent → Gate → Decision → Executor → Effect
```

Two related but distinct flows through the same `Gate`:

- **Exercising authority** — every effect-producing action (a tool call, a delegated sub-agent's
  own action, an external fetch) is the same kind of thing: an `Intent` submitted to `Gate`, which
  mints a cryptographically-bound `Decision` before `Executor` dispatches it to an execution-class
  backend.
- **Granting authority** — a *distinct* `Gate` operation (`issue_order`/`grant_root_authority`/
  `delegate` — not an `Intent` kind): one principal deriving constrained `Authority` for another,
  narrower than what it itself holds, traceable to a real originating `Order`. An `Intent` is an
  *attempted exercise* of authority, never its source — the two are deliberately not the same
  primitive. An earlier design draft treated delegation as just another `Intent` kind; that was a
  category error, corrected before it shipped (see `HISTORY.md`).

- **`siphonophore_core/`** — `Intent`/`Effect`, `Order`/`Authority`/`Scope` (`authority.py`),
  `Policy`/`Decision`/`Gate` (`mediation.py`), `Executor` and its execution-class backends
  (`same_process`, `separate_process`, `uid_cgroup`, `uid_cgroup_checkin`), `identity` (check-in
  verification: nonce + `SO_PEERCRED`), `audit` (Belnap four-valued reconciliation between
  self-report and independently-observed ground truth).
- **`spawn_helper/`** — `siphonophore-spawn`, a minimal C privileged helper letting an unprivileged
  broker use the `uid_cgroup` tiers without running as root; its pinned wire interface lives in
  `contracts/spawn_helper.md`.
- **`scripts/`** — privilege-separated `useradd`/`userdel` wrapper scripts and sudoers templates
  the broker needs to stay unprivileged.
- **`siphonophore_harness/`** — a minimal cognitive loop (prompt → completion → parse intent →
  dispatch → feed back), a real Anthropic-backed `Model`, and the system prompt that teaches a
  model the intent protocol.
- **`examples/repl.py`** — an interactive script driving a real model through the harness.

Full design is in `DESIGN.md`. `HISTORY.md` holds the narrative of how it was built, if useful,
but isn't required reading to use this.

## Requirements

- Python ≥ 3.10
- Real root on real Linux (with cgroup v2) for the `uid_cgroup`/`uid_cgroup_checkin` execution
  tiers and their tests — everything else is portable.
- A C11-capable `cc` only if you're building `spawn_helper/siphonophore-spawn` — optional, not
  needed for the Python SDK or its own test suite.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

For the real-model harness (`examples/repl.py`, `AnthropicAPIModel`):

```bash
.venv/bin/pip install -e ".[anthropic]"
export ANTHROPIC_API_KEY=sk-...
```

## Running the tests

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -v
```

Tests are split by what they need:

- Portable tests run anywhere and need nothing extra.
- Tests marked `linux_root_only` need real root on real Linux with cgroup v2 (they're skipped
  automatically everywhere else) — real `useradd`/cgroup provisioning, real privilege drops, real
  concurrent check-ins over a Unix socket, and (for `spawn_helper/`) a real compile-and-run of the
  privileged C helper. No portion of this is mocked or simulated.

## Running the live harness

```bash
.venv/bin/python examples/repl.py --model <a current Anthropic model id>
```

Type messages at the prompt; each one is a real turn — model call → intent parsing → Gate →
Executor. Pass `--verbose` to also see the raw completion and `Effect` detail instead of just the
model's conversational reply. This drives the authority-less path only (see "Current state" above)
— there's no delegation demo here yet.

## License

Apache 2.0 — see `LICENSE`.
