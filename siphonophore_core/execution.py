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

import hmac
import json
import subprocess
import sys
from abc import ABC, abstractmethod

from .intent import Effect, Intent
from .mediation import GateViolation, digest_of
from .policy import Decision


class ExecutionError(RuntimeError):
    """A backend failed to produce the effect for a reason unrelated to authorization (the
    artifact itself raised, a subprocess exited nonzero, ...). Distinct from GateViolation, which
    means the Decision itself was never trustworthy enough to reach a backend at all."""


class ArtifactMismatchError(GateViolation):
    """The code about to run does not hash to what the Decision actually authorized (DESIGN.md
    section 8; lab/008, lab/009). Raised before any backend is invoked -- lab/009 confirmed this
    ordering specifically so a rejected swap costs nothing in backend-side side effects (e.g. no
    real uid/cgroup provisioned for code that was never going to be trusted)."""


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
    isolated class -- DESIGN.md section 2's default for low-consequence, trusted-input work."""

    def run(self, decision: Decision, intent: Intent) -> Effect:
        if intent.artifact_code is None:
            raise ExecutionError("same_process backend requires intent.artifact_code")
        namespace: dict = {"payload": intent.payload}
        exec(intent.artifact_code, namespace)  # noqa: S102 -- the whole point: run exactly the authorized code
        return Effect(intent_id=intent.intent_id, execution_class="same_process", detail={})


class SeparateProcessBackend(ExecutionBackend):
    """Runs `intent.artifact_code` as a real, separate OS process (`subprocess.run`) -- real
    process isolation, no uid change. `intent.payload` is passed as a single JSON-encoded argv
    argument."""

    def run(self, decision: Decision, intent: Intent) -> Effect:
        if intent.artifact_code is None:
            raise ExecutionError("separate_process backend requires intent.artifact_code")
        try:
            proc = subprocess.run(
                [sys.executable, "-c", intent.artifact_code, json.dumps(intent.payload)],
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise ExecutionError(f"separate_process artifact exited {exc.returncode}: {exc.stderr}") from exc
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
            raise GateViolation("decision failed Gate verification -- forged, tampered, or downgraded")
        if not decision.permitted:
            raise GateViolation(f"intent {decision.intent_id!r} was not permitted by policy")

        if intent.artifact_code is not None:
            actual_digest = digest_of(intent.artifact_code)
            if not hmac.compare_digest(actual_digest, decision.artifact_digest):
                raise ArtifactMismatchError(
                    f"artifact digest mismatch: decision authorized {decision.artifact_digest[:12]}..., "
                    f"but the code about to run hashes to {actual_digest[:12]}..."
                )

        backend = self._backends.get(decision.execution_class)
        if backend is None:
            raise GateViolation(f"no backend registered for execution_class={decision.execution_class!r}")
        return backend.run(decision, intent)
