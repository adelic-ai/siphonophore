# siphonophore -- status

Built by an unattended session against `build-prompt.md`'s Phase 1 (orchestrator skeleton). Read
`DESIGN.md` first for the full reasoning this implements; this file only records what got built,
the one open design call, and what's left for a human to validate on real Linux. (Phase 2, if it
lands, gets its own section appended below in a follow-up commit -- see build-prompt.md's
"only if that lands clean with time to spare".)

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

## What still needs a human on real Linux, as root

Everything that touches `identity.provision_identity` (real `useradd`, real cgroupfs) is untestable
in this environment (macOS, unprivileged) and was NOT faked, mocked, or claimed working. Written,
wired end to end, and marked `linux_root_only` (same pattern as `test_identity.py`/
`test_checkin.py`), left for a human to run on colima the way the two existing primitives already
were:

- `tests/test_orchestrator.py::test_severed_node_dispatch_runs_under_the_provisioned_uid_end_to_end`
  -- the actual proof of the whole severed pipeline: a severed node reports its own `os.getuid()`
  back, asserted to land in the reserved node range (`identity.NODE_UID_MIN`-`NODE_UID_MAX`) and
  to differ from the broker's own uid.
- `tests/test_orchestrator.py::test_severed_node_that_never_checks_in_times_out_and_is_reported_failed`
  -- a real severed spawn against an absurdly short `checkin_timeout`, proving `CheckinTimeoutError`
  actually fires and is reported as that node's own failure, not a hang.

What *was* validated for real, unprivileged, right here, and is worth naming explicitly since it's
easy to undercount: non-severed dispatch (success and failure paths), all node-registration and
recipe-resolution validation, `_await_checkin`'s real timeout enforcement (a real `CheckinServer`,
a real deadline, no fabricated instant-return), and the full severed-runner pipeline minus the
actual uid switch and cgroup (in-process call to `_severed_runner.main()`, and a real
`python -m siphonophore._severed_runner` subprocess spawn -- both exercise real check-in client
calls, real recipe resolution, real `Agent.stream_async`, and a real stdout round trip). The one
thing genuinely untested anywhere in this session is the literal privilege drop
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

## Test status at last commit

37 collected (identity: 6, checkin: 5, orchestrator: 23, severed_runner: 3). 27 pass, 10 correctly
skipped (8 pre-existing identity/checkin skips + the 2 new `linux_root_only` severed-dispatch
tests; none faked, none silently xfail'd). Re-run: `./.venv/bin/python -m pytest -v` from the repo
root.

## Explicitly left incomplete / for a human

- The two `linux_root_only` severed-dispatch integration tests above -- run on colima as root,
  same as the prior session's identity/checkin validation.
- Whether `AgentResult.to_dict()` should itself round-trip metrics is worth raising with the
  Strands maintainers per DESIGN.md's "legitimate scoped upstream contribution" framing -- not
  initiated here; stays a human decision per build-prompt.md.
- Phase 2 (substrate-selection interface) -- not started yet in this commit; see build-prompt.md
  for scope if a follow-up session takes it on.
- The org/firm layer above the individual principal, and actually reaching out to Strands
  maintainers -- both explicitly out of scope for this prompt, untouched.
