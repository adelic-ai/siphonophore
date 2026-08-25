# siphonophore — build prompt (hands-off, to the degree that's actually honest)

You are extending `siphonophore`, a `MultiAgentBase`-compatible orchestrator for the Strands
Agents SDK that gives individual delegated nodes real OS identity, risk-tiered isolation, and
independent ground truth. Read `DESIGN.md` first — it has the full reasoning, checked against
Strands' actual source, not assumed. This prompt only covers what to build next; it does not
repeat the design reasoning already written down there.

**Read this section before doing anything else — the honest limit on "hands-off" here.** Two
already-built primitives (`siphonophore/identity.py`, `siphonophore/checkin.py`) needed real root
on real Linux to validate for real — cgroup creation, `useradd`, `SO_PEERCRED` — and were, on
colima, by a human running the specific privileged commands after review, not by an unattended
session with root inside a container. That precedent holds here too. Everything in this prompt
that can be built and proven correct in a normal, unprivileged Python process — the orchestrator
skeleton, the node-dispatch logic for *non-severed* nodes, the design decision below, tests that
don't need root — is fair game for an unattended session to just do, phase by phase, the same
"make the most reasonable call, note it in STATUS.md, keep moving" discipline `warrant`'s own
build prompt used. Anything that needs to actually spawn a process under a provisioned uid and
prove it joined the right cgroup and checked in for real needs a human to run the privileged step
on real Linux afterward — write the test, mark it `linux_root_only` the same way the existing
tests do, and stop there. Do not attempt to fake root, mock the kernel boundary, or claim a
Linux-root-only path is "done" from a Mac or an unprivileged container. That would be exactly the
self-report-without-ground-truth failure this whole project exists to not repeat, applied to its
own build process.

## What's already built — do not redo

- `siphonophore/identity.py` — uid allocation (range 59000-59899) + cgroup provisioning per node.
- `siphonophore/checkin.py` — nonce generation, `CheckinServer` (Unix socket, `SO_PEERCRED`-verified
  check-in), `check_in()` client call.
- Both validated for real on colima as root: 10/10 tests pass there; 3 platform-agnostic tests
  pass on the Mac with the rest correctly skipped, not faked.

## The one real open design question — make a call, document it, don't gloss over it

Strands' `Agent` objects aren't trivially picklable across a process boundary: tools are closures
over live objects (an HTTP client, a bound token, whatever state the registering code captured),
and the model object holds a live API client. A severed node's actual work needs to run inside a
*different* OS process than the one that built the `Agent` — so something has to cross that
boundary, and a live Python object generally can't.

The reasonable default, unless you find a better one while building: for a node that isn't being
severed (the common case per DESIGN.md's octopus/colony framing — most nodes stay part of the
shared process), dispatch exactly like Strands' own `Swarm`/`Graph` do today — call
`node.executor.stream_async(...)` in-process, no change in behavior. For a node that *is* flagged
for severing, require its registration to supply a **picklable recipe**, not a live `Agent` — e.g.
a module-level, importable callable (`factory: Callable[[], Agent]`, referenced by
`module:qualname`, the same constraint Python's own `multiprocessing` puts on picklable
callables) plus whatever serializable kwargs it needs. The spawned child process imports that
factory fresh, builds its own `Agent` locally (so nothing crosses the process boundary except the
task string, the recipe reference, and the check-in nonce), runs `stream_async` there, and streams
the result back over a pipe or the same Unix-socket mechanism `checkin.py` already establishes.

Write this decision into `STATUS.md` however you actually resolve it, including if you find a
real reason to do it differently — the point of documenting it is so the next person (or session)
doesn't have to re-derive it from scratch, not so this specific answer is treated as sacred.

## Phase 1 — the orchestrator skeleton

- A class implementing `strands.multiagent.base.MultiAgentBase` (see the installed package's
  `multiagent/base.py` for the exact abstract contract: `NodeResult`, `MultiAgentResult`, `Status`
  — read it directly, do not guess the shape). Start from something structurally similar to the
  `RemoteAgentNode` example in Strands issue #1010 (cited in `DESIGN.md`) as a reference for the
  contract shape, not as code to copy — that example routes to a remote HTTP endpoint; this routes
  to a locally-provisioned OS process instead.
- A per-node registration API distinguishing severed vs. non-severed nodes (see the design
  question above) — non-severed nodes take a live `Agent` the way `Swarm`/`Graph` already do;
  severed nodes take the picklable recipe.
- Node dispatch for the *non-severed* path: call `stream_async` in-process, full stop — this path
  needs no OS privilege and should be fully tested without root.
- Node dispatch for the *severed* path, wired end to end using the two existing primitives:
  1. `identity.provision_identity(node_id)` — before spawning anything.
  2. Generate a nonce (`checkin.generate_nonce()`), register it with a `CheckinServer`
     (`register_pending`) before spawn.
  3. Spawn the child process (`subprocess.Popen`, running as the provisioned uid — `preexec_fn` or
     the `user=` kwarg, whichever proves cleaner once you're actually writing this), passing the
     nonce and the recipe reference via argv or an inherited fd — not an env var (readable via
     `/proc/<pid>/environ` at the parent's own privilege level; see `checkin.py`'s own docstring
     for why that matters).
  4. `identity.add_pid_to_cgroup(...)` for the spawned pid.
  5. The child's first action, before touching its own recipe or task: `checkin.check_in(...)`.
  6. The orchestrator blocks (with a real timeout — an unverified node after a reasonable window
     is a failure, not a hang) on `CheckinServer.is_verified(node_id)` before treating the node as
     trustworthy, then dispatches the actual task.
  7. `identity.release_identity(...)` once the node's result is collected — cgroup first, then
     uid, per `release_identity`'s own ordering.
- Tests: everything not requiring root runs and passes on the Mac. Everything requiring root gets
  written, marked `linux_root_only`, and left for a human to actually run on colima — same as the
  existing two primitive modules.

## Phase 2 — risk-axis substrate selection (design only, unless Phase 1 lands clean)

DESIGN.md's substrate tiers (bare process+uid+cgroup / real container / microVM-or-gVisor) are a
per-node decision, not a global one. Don't build the container/microVM tiers yet — write the
*interface* a per-node substrate choice would need (something that could plausibly be swapped in
without changing the orchestrator's own dispatch logic), and leave the actual container/microVM
implementations for a separately-scoped pass, the same way `warrant` left real SPIRE deployment as
"explicitly out of scope, considered, not built" rather than half-building it. Note the call in
`STATUS.md` either way.

## Explicitly out of scope for this prompt

- Anything from `DESIGN.md`'s "org/firm layer above the individual principal" section — a real,
  separate design question, not something to bolt onto this build.
- Actually reaching out to the Strands maintainers (GitHub Discussion, issue, or otherwise) — that
  stays a human decision, not something an unattended session initiates on its own.
- Publishing this anywhere, or adding a remote — this repo has no `origin` configured on purpose;
  don't add one.

## Commit discipline

Commit after each phase, tests green (or root-only tests correctly marked and skipped) at each
commit — same discipline `warrant`'s own build prompt used. If Phase 1's design question turns out
to need more exploration than expected, stop at a clean, honestly-documented partial state rather
than forcing a shaky end-to-end path just to call the phase done.
