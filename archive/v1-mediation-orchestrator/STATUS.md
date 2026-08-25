# siphonophore -- status

Built by an unattended session against `build-prompt.md`'s Phase 1 (orchestrator skeleton) and
Phase 2 (substrate interface, no implementation). Read `DESIGN.md` first for the full reasoning
this implements; this file only records what got built, the one open design call, and what's
left for a human to validate on real Linux.

## What's built

- `siphonophore/orchestrator.py` -- `Colony`, a `strands.multiagent.base.MultiAgentBase`
  implementation. Per-node registration distinguishes non-severed (`add_node`, a live `Agent`,
  dispatched in-process exactly like Strands' own `Swarm`/`Graph`) from severed (`add_severed_node`,
  a `SeveredRecipe`, dispatched inside its own OS-provisioned process). `invoke_async` fans out to
  every registered node (or a caller-selected subset via `invocation_state={"nodes": [...]}`)
  concurrently against the same task and merges results into one `MultiAgentResult`.
- `siphonophore/_severed_runner.py` -- the entrypoint a severed node's spawned child actually runs
  (`python -m siphonophore._severed_runner <socket> <nonce> <payload_json>`). Checks in first,
  before touching its recipe or task; resolves and calls the recipe; emits a result envelope on
  stdout.
- `siphonophore/testing.py` -- a minimal deterministic `strands.models.model.Model` test double
  (`StubModel`) and three module-level factory functions (`make_stub_agent`,
  `make_failing_agent`, `make_uid_reporting_agent`) usable both as plain test `Agent`s and as
  `SeveredRecipe.factory` references, since a severed node's recipe must be importable by
  reference, not a live object.
- `siphonophore/substrate.py` -- Phase 2, interface only (see below).
- `siphonophore/__init__.py` -- exports `Colony`, `SeveredRecipe`, and the orchestrator's own
  exception types.
- `pyproject.toml` -- added `strands-agents>=1.53.0` as a real dependency; installed in this
  repo's own `.venv` (`strands-agents 1.53.0`, matching the version already installed in
  `warrant/.venv`, which is what `multiagent/base.py`, `swarm.py`, `graph.py`, `agent_result.py`,
  `types/streaming.py` etc. were all read from directly -- not guessed -- to get the exact
  `MultiAgentBase`/`NodeResult`/`AgentResult`/`Model` contracts right).

## The open design question: picklable recipe, not a live `Agent` -- as proposed, no changes found

`build-prompt.md` proposed a default and asked me to confirm or correct it while actually building.
Confirmed as-is: a severed node's registration takes a `SeveredRecipe(factory="module:qualname",
kwargs={...})`, resolved (imported) once at *registration* time to fail fast on a bad reference
(`resolve_factory` in `orchestrator.py`), and resolved again *inside the spawned child* to actually
build the `Agent` there. Nothing about the live `Agent` object crosses the process boundary --
only the task, the recipe reference + kwargs, and the check-in nonce, all via argv, all JSON.

One thing worth flagging that build-prompt.md's description didn't fully anticipate:
`resolve_factory` rejects a recipe that resolves to a closure or lambda (anything whose
`__qualname__` contains `"<locals>"`), not just anything unimportable. A closure re-imports to a
*different* function object with an unresolvable enclosing scope in a fresh child process -- this
needed catching at registration time, with a clear message naming the actual problem, rather than
surfacing as a mysterious `NameError`/`AttributeError` inside an already-spawned child that has no
clean channel to report a resolution failure back to the broker that provisioned it. Tested for
real (`test_resolve_factory_rejects_a_closure_because_a_fresh_import_cannot_see_its_scope`), no
root needed -- it's a pure import-time check.

## A gap found while building, not anticipated by DESIGN.md or build-prompt.md

Checked against the installed package, not assumed: `strands.agent.agent_result.AgentResult.to_dict()`
/ `from_dict()` deliberately only round-trip `message`/`stop_reason`/`checkpoint` -- built for
session persistence, not full-fidelity IPC. `from_dict` always reconstructs a fresh, empty
`EventLoopMetrics()`. Sending a severed node's `AgentResult.to_dict()` back over stdout *alone*
would silently zero out that node's real token usage and latency crossing the process boundary --
a `NodeResult` that looks complete but is quietly wrong about the numbers, exactly the kind of
self-report-shaped failure this whole project exists to not repeat, just moved into the plumbing
instead of the agent's own claims.

Worked around, not left broken: `_severed_runner._build_envelope` sends a small JSON envelope --
`{"agent_result": ..., "usage": ..., "metrics": ..., "interrupts": [...]}` -- carrying
usage/metrics/interrupts as their own top-level fields alongside (not instead of) the standard
`agent_result.to_dict()` payload; `orchestrator.parse_runner_output` reconstructs all four
independently. Tested for real, no root needed
(`test_parse_runner_output_round_trips_a_real_agent_result`, and end to end via
`test_severed_runner.py`'s subprocess test).

This is plausibly exactly the class of thing DESIGN.md names as legitimate scoped upstream
material -- "a specific gap in the `MultiAgentBase`/hook contract... a small, reviewable fix" --
though whether `AgentResult.to_dict()` *should* round-trip metrics is a question for whoever
actually reaches out to the Strands maintainers, which build-prompt.md is explicit stays a human
decision.

## Phase 2: substrate interface, deliberately not wired into Phase 1's dispatch

`siphonophore/substrate.py` defines `Substrate` (an abstract `spawn`/`collect`/`teardown`
interface) and three implementations:

- `ProcessSubstrate` -- real, functionally identical to what `Colony._dispatch_severed` already
  does inline. Exists to prove the interface shape actually fits the one tier that's built.
- `ContainerSubstrate`, `MicroVMSubstrate` -- honest stubs. Every method raises
  `NotImplementedError` naming itself. Not built, per build-prompt.md's explicit instruction not
  to build the container/microVM tiers yet -- same "explicitly out of scope, considered, not
  built" discipline `warrant` used for real SPIRE deployment.

Deliberate scope call, not an oversight: **`Colony._dispatch_severed` does NOT route through
`Substrate`/`ProcessSubstrate`** -- it still does the bare-process work inline, exactly as Phase 1
built and validated it. Routing Phase 1's already-green severed dispatch through this interface,
for a single tier, with zero behavior change, would mean re-touching and re-validating tested code
for no functional gain -- the actual point of pulling the interface out (letting a node pick its
own substrate) only pays for itself once a second tier exists to choose between. Left for whoever
builds `ContainerSubstrate` or `MicroVMSubstrate` for real: wire `Colony` through `Substrate` at
the same time, validated once, together with an actual reason to abstract over more than one
implementation.

## Validated on real Linux, as root (2026-08-24, colima) -- a real bug was found and fixed here

Both `linux_root_only` severed-dispatch integration tests were run for real on colima as root, the
same way `identity.py`/`checkin.py` were validated before this orchestrator was built on top of
them. The first run genuinely failed -- this is exactly the case for not letting an unattended,
unprivileged session claim this path works:

`test_severed_node_dispatch_runs_under_the_provisioned_uid_end_to_end` failed with a real
`CheckinTimeoutError`. Root cause, found by reproducing directly and reading the child's actual
stderr (which the orchestrator's own timeout path doesn't surface, since it kills the process
before `communicate()`): `CheckinServer.start()`'s `socket.bind()` creates the check-in socket with
the broker's own umask permissions (0755) -- a freshly provisioned node's uid is neither the
socket's owner nor in its owning group, so it has no write permission on the socket file, and
`AF_UNIX` `connect()` requires write permission on the target. Every severed node's check-in was
silently, permanently unable to reach the broker at all; it would have timed out identically in a
real deployment, not just in the test.

Fixed in `checkin.py`: `os.chmod(socket_path, 0o777)` after bind. This does not weaken
verification -- `_handle`'s nonce + `SO_PEERCRED`-verified-uid check is the actual security
boundary and silently drops any connection that doesn't match both, regardless of how it reached
`accept()`. Re-ran after the fix: the severed node's own reported uid was `59000`, inside the
reserved range and distinct from the broker's own (root's) uid -- the real, unambiguous proof the
child process actually ran as the provisioned identity. **40/40 tests pass on colima as root now**
(was 39 passed / 1 failed / 2 skipped before the fix); 2 correctly skip there (the
without-real-privilege test, which needs to run *without* root to prove that path, and the
non-Linux `SO_PEERCRED` test).

What *was* validated for real, unprivileged, right here, and is worth naming explicitly since it's
easy to undercount: non-severed dispatch (success and failure paths), all node-registration and
recipe-resolution validation, `_await_checkin`'s real timeout enforcement (a real `CheckinServer`,
a real deadline, no fabricated instant-return), the full severed-runner pipeline minus the actual
uid switch and cgroup (in-process call to `_severed_runner.main()`, and a real
`python -m siphonophore._severed_runner` subprocess spawn -- both exercise real check-in client
calls, real recipe resolution, real `Agent.stream_async`, and a real stdout round trip), and
`ProcessSubstrate`'s spawn/collect/teardown against a real (self-uid, not fabricated) `Identity`.
The one thing genuinely untested anywhere in this session is the literal privilege drop
(`user=<provisioned uid>`) succeeding against a uid that isn't the caller's own, plus
`identity.provision_identity`/`add_pid_to_cgroup` themselves -- both already validated in the
prior session per `build-prompt.md`'s own account (10/10 on colima as root), just not re-proven
here in combination with the new dispatch code around them.

Also confirmed, deliberately, as a real (not fabricated) result: attempting severed dispatch
*without* real privilege on this machine fails exactly as it should --
`identity.provision_identity` raises `FileNotFoundError: 'useradd'` (no such command on macOS),
propagated cleanly as that one node's own `FAILED` `NodeResult`, not a crash, not a silent
success. See `test_severed_dispatch_without_real_privilege_fails_cleanly_as_that_nodes_own_failure`
(runs everywhere this repo's tests run, including right here).

## A second real bug, found by questioning the design out loud, not by more testing

After the fix above, the check-in nonce still traveled via argv (`build_argv`'s own docstring
claimed this was safer than an env var -- "an env var is readable via `/proc/<pid>/environ` at the
parent's own privilege level"). Asked directly: is nonce-plus-kernel-verified-uid actually
principled, or just a hack layered on top of already-real primitives? Checked the claim itself
empirically rather than defend it by assertion -- `ls -la /proc/<pid>/cmdline /proc/<pid>/environ`
and a cross-uid read attempt (`sudo -u nobody cat ...`) on the real colima host, not reasoned about
in the abstract:

    -r--r--r-- 1 shunhonda shunhonda 0 ... /proc/6318/cmdline    <- world-readable
    -r-------- 1 shunhonda shunhonda 0 ... /proc/6318/environ    <- owner-only
    $ sudo -u nobody cat /proc/6318/cmdline   -> succeeds, prints the full command line
    $ sudo -u nobody cat /proc/6318/environ   -> Permission denied

The opposite of what the code assumed. `/proc/<pid>/cmdline` is world-readable by default on real
Linux; `/proc/<pid>/environ` is actually the more protected of the two. A nonce placed in argv was
readable by any local process, any uid, for the severed node's entire lifetime -- worse than the
env var it was deliberately avoided in favor of, not better. (The nonce+uid *design* itself was
never the hack, and remains right: `_handle`'s actual security boundary is the kernel-verified peer
uid, with the nonce only ever used as an opaque lookup key -- a leaked nonce alone, without also
controlling the matching uid, verifies nothing. What was sloppy was the *transport*, not the
protocol.)

Fixed: the nonce now travels via an inherited pipe file descriptor (`os.pipe()`, the read end
passed through `pass_fds` at spawn, the write end written-and-closed by the broker before spawn) --
readable by nothing outside that pipe's own two ends, regardless of uid. `Substrate.spawn`'s
interface gained a `pass_fds` parameter to carry this correctly through Phase 2's abstraction too,
not just Phase 1's inline dispatch (`ProcessSubstrate` forwards it; the two unbuilt tiers' stub
signatures were updated to match so the interface stays consistent). Re-validated end to end on
colima as root after the fix, including that a root-created pipe fd is correctly inherited by a
child spawned under a *different*, lower-privileged uid via `pass_fds` combined with `user=` --
not something to assume works without checking, since fd inheritance across a real privilege
switch is exactly the kind of thing that could plausibly have its own permission quirks. It didn't:
40/40 pass on colima as root, 32/32 (with 10 correctly skipped) on the Mac, both re-run after this
fix, not just before it.

## Test status at last commit

42 collected (identity: 6, checkin: 5, orchestrator: 23, severed_runner: 3, substrate: 5 --
4 test functions, one parametrized across both unbuilt substrate tiers). On the Mac (unprivileged,
macOS): 32 pass, 10 correctly skipped. On colima (real Linux, real root): 40 pass, 2 correctly
skipped -- see the validation section above for the one real bug that run found and the fix.
None faked, none silently xfail'd, on either platform. Re-run: `./.venv/bin/python -m pytest -v`
from the repo root (Mac), or as root on a real Linux host for the full suite.

## Explicitly left incomplete / for a human

- Whether `AgentResult.to_dict()` should itself round-trip metrics is worth raising with the
  Strands maintainers per DESIGN.md's "legitimate scoped upstream contribution" framing -- not
  initiated here; stays a human decision per build-prompt.md.
- `ContainerSubstrate`/`MicroVMSubstrate` real implementations, and wiring `Colony` through
  `Substrate` generally -- separately scoped, per build-prompt.md's explicit instruction.
- The org/firm layer above the individual principal, and actually reaching out to Strands
  maintainers -- both explicitly out of scope for this prompt, untouched.
