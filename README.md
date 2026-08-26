# siphonophore

SDK for mediated, attributable agent execution — every tool call and delegation passes through
one cryptographically-bound authority boundary, with real OS-level execution identity and
independent, kernel-verified ground truth.

## Why this exists

Existing agent SDKs (Strands and others) unify how a tool call and a sub-agent delegation are
*invoked*, but not how they're *authorized* — a sub-agent still runs in-process, sharing the
parent's uid/pid, with no independent way to attribute what it actually did versus what it claims
to have done. That gap matters more as agentic systems take on higher-stakes, less-supervised work
(CI/CD automation, delegated sub-tasks, unattended runs) — exactly the direction security
expectations for these systems are heading. siphonophore is not a framework for building agents
faster; it's one that makes an agent's actions structurally impossible to leave unattributed.

## Architecture

```
Principal → Intent → Gate (Decision) → Executor → Effect
```

Every effect-producing action — a tool call, a delegation, an external fetch — is the same kind of
thing: an `Intent` submitted to a `Gate`, which mints a cryptographically-bound `Decision` before
an `Executor` dispatches it to an execution-class backend. Delegation reduces to the identical
primitive a tool call uses; nothing is special-cased by kind.

- **`siphonophore_core/`** — `Intent`/`Effect`, `Policy`/`Decision`/`Gate`, `Executor` and its
  execution-class backends (`same_process`, `separate_process`, `uid_cgroup`,
  `uid_cgroup_checkin`), `identity` (check-in verification: nonce + `SO_PEERCRED`), `audit`
  (Belnap four-valued reconciliation between self-report and independently-observed ground truth).
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
  concurrent check-ins over a Unix socket. No portion of this is mocked or simulated.

## Running the live harness

```bash
.venv/bin/python examples/repl.py --model <a current Anthropic model id>
```

Type messages at the prompt; each one is a real turn — model call → intent parsing → Gate →
Executor. Pass `--verbose` to also see the raw completion and `Effect` detail instead of just the
model's conversational reply.

## Status

Core mediation, execution tiers through `uid_cgroup`/`uid_cgroup_checkin`, check-in, and Belnap
reconciliation are built and tested, including for real on root Linux, not just against fakes.
Not yet built: `container`/`VM` execution tiers, platform attestation, a real credential-delivery
mechanism, multi-model support beyond `Model` being swappable in principle. `DESIGN.md`'s
"Explicitly open" section names every unresolved question honestly, including this project's own
disclosed limitations (e.g. `consequence` is currently caller-declared, not independently
verified) — read it before assuming a guarantee holds that isn't actually enforced yet.

## License

Apache 2.0 — see `LICENSE`.
