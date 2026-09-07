"""Executor -- dispatches a verified Decision to whatever actually performs the effect
(DESIGN.md section 2).

Never trusts a Decision because one was handed to it -- independently re-verifies it, and
independently re-checks execution provenance (DESIGN.md section 8), before any backend runs.

Execution class is a real extension point (DESIGN.md section 6's "executor/substrate backend"):
`ExecutionBackend` is the interface a new class (a container or VM tier, say) implements to be
registered, rather than another branch in a growing if/elif chain -- the shape every lab script
necessarily used, being self-contained scripts, but not the right shape for real, extensible code.

Calling convention for `intent.payload`, standardized here (the lab scripts used ad-hoc,
per-experiment conventions -- a bound `path` variable for same_process, a bare argv for
separate_process/uid_cgroup): `payload` is always a JSON-serializable dict. `same_process` binds it
as a local variable named `payload` in the exec namespace. `separate_process` and `uid_cgroup` pass
it as a single JSON-encoded argv argument; artifact code targeting those classes is responsible for
`json.loads(sys.argv[1])`.
"""
from __future__ import annotations

import contextlib
import hmac
import io
import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod

from .intent import Effect, Intent
from .mediation import GateViolation, digest_of
from .policy import Decision

# Bound on captured same_process stdout/stderr, characters not bytes (truncating on a character
# boundary avoids splitting a multi-byte UTF-8 sequence, which byte-slicing a `str` cannot do
# safely). No existing precedent in this codebase to match -- `SeparateProcessBackend` has never
# bounded `proc.stdout` (docs/REFERENCE_HARNESS_V1_HORIZON.md's own next-stage scope explicitly
# limits this pass to same_process only, leaving that pre-existing, lower-priority gap open, not
# regressed). 100,000 characters is a deliberately generous, round bound: large enough that no
# ordinary diagnostic/inspection output is ever truncated, small enough that a runaway or
# adversarial artifact cannot grow `Effect.detail` (and everything that holds one -- CognitiveLoop
# history, this process's own memory) without bound merely because output is now captured instead
# of streamed directly to a real terminal.
_MAX_CAPTURED_OUTPUT_CHARS = 100_000


def _truncate_captured_output(text: str) -> str:
    """Truncate `text` to `_MAX_CAPTURED_OUTPUT_CHARS`, appending an explicit, unambiguous marker
    when truncation actually happened -- never silently drop the tail. A caller (or a human reading
    `Effect.detail`) must never mistake a truncated capture for the artifact's complete output."""
    if len(text) <= _MAX_CAPTURED_OUTPUT_CHARS:
        return text
    return (
        text[:_MAX_CAPTURED_OUTPUT_CHARS]
        + f"\n...[truncated, {len(text)} characters total, {_MAX_CAPTURED_OUTPUT_CHARS} shown]"
    )


class ExecutionError(RuntimeError):
    """A backend failed to produce the effect for a reason unrelated to authorization (the
    artifact itself raised, a subprocess exited nonzero, ...). Distinct from GateViolation, which
    means the Decision itself was never trustworthy enough to reach a backend at all."""


def _running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


_ROOT_REFUSAL_MESSAGE = (
    "{cls} refuses to run while the broker process is euid 0 (root). {cls} inherits whatever "
    "privilege the broker itself has -- a broker running as root (needed for uid_cgroup/"
    "uid_cgroup_checkin) would otherwise let a 'low consequence' intent run with full root "
    "privilege and zero isolation, since {cls} shares the broker's own process/inherited "
    "privilege with no privilege drop of its own. Pass allow_root=True only if you have "
    "independently verified this is safe for your deployment -- it is never the default."
)


class ArtifactMismatchError(GateViolation):
    """The code about to run does not hash to what the Decision actually authorized (DESIGN.md
    section 8; lab/008, lab/009). Raised before any backend is invoked -- lab/009 confirmed this
    ordering specifically so a rejected swap costs nothing in backend-side side effects (e.g. no
    real uid/cgroup provisioned for code that was never going to be trusted)."""


class DecisionVerificationError(GateViolation):
    """The Decision itself failed Gate.verify() -- forged, tampered, or downgraded after minting.
    Distinct from an ordinary policy DENY: a DENY is a real, signed "no" from a Decision that
    verifies correctly; this means the Decision handed to Executor cannot be trusted at all,
    regardless of what it claims about `permitted`."""


class PolicyDeniedError(GateViolation):
    """A Decision that verifies correctly (not forged/tampered) but whose `permitted` field is
    False -- the ordinary, expected policy refusal. Distinct from DecisionVerificationError: this
    Decision is genuinely the one the Gate minted, and the Gate's own answer was no."""


class NoBackendRegisteredError(GateViolation):
    """No ExecutionBackend is registered for the Decision's `execution_class` on this Executor --
    a configuration/environment gap, not an authorization judgment. Distinct from a policy
    decision: the Gate already permitted this intent and chose an execution_class: this Executor
    simply has nothing registered to carry it out."""


class ExecutionBackend(ABC):
    """One execution class's actual dispatch logic. `Executor` delegates to whichever backend is
    registered for `decision.execution_class` -- implement this to add a new class (a container or
    VM tier) without touching `Executor` itself."""

    @abstractmethod
    def run(self, decision: Decision, intent: Intent) -> Effect:
        """Perform the effect. Called only after Executor has already verified the Decision and
        (if intent.artifact_code is set) confirmed its digest matches -- a backend does not need
        to re-check either, and should not skip straight to side effects if it does."""
        ...


class SameProcessBackend(ExecutionBackend):
    """Runs `intent.artifact_code` via `exec()` in the calling process. The cheapest, least
    isolated class -- DESIGN.md section 2's default for low-consequence, trusted-input work.

    Refuses to run at all while the broker is euid 0 by default (see `allow_root`) -- `exec()`
    here runs with exactly the broker's own privilege, no drop of any kind, so a root broker
    (needed for the uid_cgroup tiers) would otherwise hand a "low consequence" intent full root.

    **Output capture (docs/REFERENCE_HARNESS_V1_HORIZON.md's "next bounded implementation
    stage").** `exec()` runs with `sys.stdout`/`sys.stderr` redirected into in-memory buffers for
    the duration of the call, and whatever the artifact wrote is placed into `Effect.detail`
    (`"stdout"`/`"stderr"`, present only when non-empty -- an ordinary artifact with no output
    still produces `detail == {}`, unchanged from before this capture existed) instead of reaching
    this process's real stdout/stderr streams. This closes a real, previously-reproduced defect: an
    artifact's `print()`/traceback output used to write directly to whatever real terminal this
    process happened to share (e.g. a REPL operator's own terminal), unbounded and unmediated,
    because `same_process` runs in-process and inherited the process's real streams with no
    redirection at all. Capturing is a presentation/evidence-boundary correction, not a change to
    isolation or authorization: the code that runs is still exactly what the Decision authorized
    (digest-checked below by `Executor.execute()`, unchanged) -- what changes is only where its
    *output* goes afterward, mirroring `SeparateProcessBackend`'s own `Effect.detail["stdout"]`
    shape (added here as an explicit `"stderr"` key too, which `SeparateProcessBackend` does not
    currently expose -- see that class's own docstring for why its stderr is discarded on success).

    **Concurrency/reentrancy limitation, disclosed precisely, not silently claimed away:**
    `contextlib.redirect_stdout`/`redirect_stderr` mutate `sys.stdout`/`sys.stderr` at module/process
    scope for the duration of the `with` block -- not per-thread, per-task, or per-instance state.
    Two `SameProcessBackend.run()` calls genuinely running concurrently on different threads of the
    *same process* would each redirect the same process-global streams, and each would risk
    observing (or losing) the other's output -- this backend makes no attempt to serialize or
    thread-isolate that, and nothing in `Executor`, `Broker`, or `CognitiveLoop` today calls `run()`
    from more than one thread at a time (every existing caller -- `CognitiveLoop.step()`,
    `Executor.execute()`, every test in this suite -- dispatches strictly one intent at a time,
    synchronously). This capture mechanism is therefore correct for that existing, single-dispatch-
    at-a-time usage and is NOT safe for concurrent same_process execution from multiple threads
    sharing one process -- a future caller introducing real concurrency across `same_process`
    dispatches on a shared process would need a different mechanism (e.g. per-call thread-local
    stream substitution is not sufficient either, since `sys.stdout` itself is not thread-local;
    genuine isolation would need a separate process, which is exactly what `SeparateProcessBackend`
    already is). This limitation is not new to this stage -- `exec()` sharing the process's real,
    global streams was already true before capture existed; capturing does not introduce the
    limitation, it just makes it worth stating explicitly now that streams are being read as well
    as written.

    **Artifact exceptions (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md coherence fix).** An artifact
    that raises is caught and re-raised as `ExecutionError` (`raise ... from exc`, preserving the
    original as `__cause__`), matching `SeparateProcessBackend`'s own `subprocess.CalledProcessError`
    handling -- both backends implementing `ExecutionBackend` now raise the same exception family
    on an artifact's own failure, not one wrapped and the other raw. Whatever was captured into the
    stdout/stderr buffers before the exception is attached to the raised `ExecutionError` as a
    plain `.detail` attribute (the same "attach curated data to the exception" pattern `broker.py`
    already uses for `DecisionProjection`) -- so a caller can still see what the artifact printed
    before it crashed, rather than that evidence being silently discarded."""

    def __init__(self, allow_root: bool = False) -> None:
        self._allow_root = allow_root

    def run(self, decision: Decision, intent: Intent) -> Effect:
        if not self._allow_root and _running_as_root():
            raise ExecutionError(_ROOT_REFUSAL_MESSAGE.format(cls="same_process"))
        if intent.artifact_code is None:
            raise ExecutionError("same_process backend requires intent.artifact_code")
        namespace: dict = {"payload": intent.payload}
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                exec(intent.artifact_code, namespace)  # noqa: S102 -- the whole point: run exactly the authorized code
        except Exception as exc:
            # Wrapped into ExecutionError -- matching SeparateProcessBackend's own
            # subprocess.CalledProcessError handling below, a deliberate coherence fix: two
            # backends implementing the same ExecutionBackend interface should raise the same
            # exception family on an artifact's own failure, not one wrapped and the other raw
            # (the raw-passthrough behavior was previously disclosed and deliberate, scoped out of
            # the output-capture stage; V1's continuation architecture needs EXECUTION_FAILED to be
            # reachable identically from either backend, which requires this). Whatever was
            # captured before the exception is attached as `.detail`, exactly as a successful run's
            # Effect.detail would carry it -- this also resolves that stage's own disclosed
            # limitation ("partial output... discarded... no Effect is constructed on the failure
            # path, so there is nothing to attach it to"): there IS somewhere to attach it now,
            # this exception, following the same "attach curated data to the exception" pattern
            # broker.py already uses for DecisionProjection.
            partial_detail: dict = {}
            stdout = stdout_buffer.getvalue()
            stderr = stderr_buffer.getvalue()
            if stdout:
                partial_detail["stdout"] = _truncate_captured_output(stdout)
            if stderr:
                partial_detail["stderr"] = _truncate_captured_output(stderr)
            error = ExecutionError(f"same_process artifact raised {type(exc).__name__}: {exc}")
            error.detail = partial_detail
            raise error from exc
        detail: dict = {}
        stdout = stdout_buffer.getvalue()
        stderr = stderr_buffer.getvalue()
        if stdout:
            detail["stdout"] = _truncate_captured_output(stdout)
        if stderr:
            detail["stderr"] = _truncate_captured_output(stderr)
        return Effect(intent_id=intent.intent_id, execution_class="same_process", detail=detail)


class SeparateProcessBackend(ExecutionBackend):
    """Runs `intent.artifact_code` as a real, separate OS process (`subprocess.run`) -- real
    process isolation, no uid change. `intent.payload` is passed as a single JSON-encoded argv
    argument.

    Refuses to run at all while the broker is euid 0 by default (see `allow_root`) -- a genuinely
    separate process is not the same thing as a genuinely unprivileged one: with no `user=`/
    `preexec_fn` privilege drop, this spawned process still runs as root if the broker does."""

    def __init__(self, allow_root: bool = False) -> None:
        self._allow_root = allow_root

    def run(self, decision: Decision, intent: Intent) -> Effect:
        if not self._allow_root and _running_as_root():
            raise ExecutionError(_ROOT_REFUSAL_MESSAGE.format(cls="separate_process"))
        if intent.artifact_code is None:
            raise ExecutionError("separate_process backend requires intent.artifact_code")
        try:
            proc = subprocess.run(
                [sys.executable, "-c", intent.artifact_code, json.dumps(intent.payload)],
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as exc:
            # .detail attached for the same reason SameProcessBackend's own exception handling
            # does (this class's docstring; broker.py's DecisionProjection precedent) -- a caller
            # can still see what the artifact printed before it exited nonzero.
            error = ExecutionError(f"separate_process artifact exited {exc.returncode}: {exc.stderr}")
            error.detail = {"stdout": exc.stdout, "stderr": exc.stderr}
            raise error from exc
        return Effect(
            intent_id=intent.intent_id, execution_class="separate_process",
            detail={"acting_pid": None, "stdout": proc.stdout},
        )


class Executor:
    """Dispatches a verified, provenance-checked Decision to the registered backend for its
    execution_class. Backends for `same_process` and `separate_process` are registered by default;
    `uid_cgroup` (execution_uid_cgroup.py) is opt-in, registered explicitly by the caller, since it
    needs real root on real Linux and should never be silently assumed available."""

    def __init__(self, gate, backends: dict[str, ExecutionBackend] | None = None) -> None:
        self._gate = gate
        self._backends: dict[str, ExecutionBackend] = backends if backends is not None else {
            "same_process": SameProcessBackend(),
            "separate_process": SeparateProcessBackend(),
        }

    def register_backend(self, execution_class: str, backend: ExecutionBackend) -> None:
        self._backends[execution_class] = backend

    def execute(self, decision: Decision, intent: Intent) -> Effect:
        if decision.intent_id != intent.intent_id or decision.kind != intent.kind:
            raise GateViolation("decision does not correspond to this intent")
        if not self._gate.verify(decision):
            raise DecisionVerificationError("decision failed Gate verification -- forged, tampered, or downgraded")
        if not decision.permitted:
            raise PolicyDeniedError(f"intent {decision.intent_id!r} was not permitted by policy")

        if intent.artifact_code is not None:
            actual_digest = digest_of(intent.artifact_code)
            if not hmac.compare_digest(actual_digest, decision.artifact_digest):
                raise ArtifactMismatchError(
                    f"artifact digest mismatch: decision authorized {decision.artifact_digest[:12]}..., "
                    f"but the code about to run hashes to {actual_digest[:12]}..."
                )

        backend = self._backends.get(decision.execution_class)
        if backend is None:
            raise NoBackendRegisteredError(f"no backend registered for execution_class={decision.execution_class!r}")
        return backend.run(decision, intent)
