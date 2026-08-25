"""008 -- execution provenance: does a bound artifact digest actually catch a swapped effect.

See `lab/008-execution-provenance.md` for the hypothesis (with explicit null), method, and
analysis. This script runs the experiment and writes results under `out/008/`.

DESIGN.md SS8 states execution provenance as a requirement: a provisioned uid/cgroup identifies
*which running process* is being observed, not whether that process is running the code the
broker actually meant to authorize. This experiment adds an `artifact_digest` to `Decision` --
bound into the HMAC the same way `kind` and `execution_class` already are (SS2's binding
discipline) -- and, critically, has `Executor.execute()` independently recompute the digest of the
code it is *about to run* and compare it to what was authorized, before running anything.

The concrete attack this closes: authorize execution of program A (Gate.submit() binds A's
digest), then present the Executor with a *different* Intent, same intent_id, but carrying program
B instead -- a swapped-artifact / time-of-check-to-time-of-use attempt. A Decision alone (kind,
execution_class, permitted) says nothing about *which code* it authorized; only a bound digest,
independently re-verified at the point of execution, can catch this.

Portable -- no root needed. This experiment is about the binding/verification logic itself, not
about execution class or privilege separation, which is why it's kept clean of both (same
discipline that kept 001-003 free of uid/cgroup complexity before 004 introduced it deliberately).

Stdlib only. No imports outside this file (DESIGN.md SS0; HISTORY.md's no-dependencies incident).

Run::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/008_execution_provenance.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "008"


# ---------------------------------------------------------------------------
# Core primitives -- same shape as 001-003, extended with artifact_digest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    kind: str  # "run_artifact"
    principal_id: str
    intent_id: str
    payload: dict
    consequence: str
    artifact_code: str  # the actual code to be executed -- this is the thing being authorized


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str
    artifact_digest: str  # sha256 hex digest of the artifact_code that was actually authorized
    token: str


def digest_of(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class Gate:
    CONSEQUENCE_TO_CLASS = {"low": "same_process", "high": "separate_process"}

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(self, intent_id, principal_id, kind, permitted, execution_class, artifact_digest) -> bytes:
        return f"{intent_id}:{principal_id}:{kind}:{permitted}:{execution_class}:{artifact_digest}".encode("utf-8")

    def _mint(self, intent_id, principal_id, kind, permitted, execution_class, artifact_digest) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted, execution_class, artifact_digest)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        permitted = self._policy(intent)
        execution_class = self.CONSEQUENCE_TO_CLASS.get(intent.consequence, "same_process")
        artifact_digest = digest_of(intent.artifact_code)
        token = self._mint(intent.intent_id, intent.principal_id, intent.kind, permitted, execution_class, artifact_digest)
        return Decision(
            intent_id=intent.intent_id, principal_id=intent.principal_id, kind=intent.kind,
            permitted=permitted, execution_class=execution_class, artifact_digest=artifact_digest, token=token,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id, decision.principal_id, decision.kind, decision.permitted,
            decision.execution_class, decision.artifact_digest,
        )
        return hmac.compare_digest(expected, decision.token)

    def _policy(self, intent: Intent) -> bool:
        return intent.kind == "run_artifact" and intent.consequence in ("low", "high")


class GateViolation(PermissionError):
    pass


class ArtifactMismatchError(GateViolation):
    """The code about to run does not hash to what the Decision actually authorized -- distinct
    from a plain GateViolation so the write-up and tests can tell a swapped-artifact attempt apart
    from a forged-token or downgrade-replay attempt."""


class Executor:
    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def execute(self, decision: Decision, intent: Intent) -> dict:
        if decision.intent_id != intent.intent_id or decision.kind != intent.kind:
            raise GateViolation("decision does not correspond to this intent")
        if not self._gate.verify(decision):
            raise GateViolation("decision failed Gate verification -- forged, tampered, or downgraded")
        if not decision.permitted:
            raise GateViolation("decision denies this intent")

        # The actual provenance check: independently recompute the digest of the code about to
        # run and compare it to what was authorized. This is not the same check as Gate.verify()
        # -- verify() confirms the Decision's OWN fields are internally consistent (nothing was
        # tampered with after minting); this confirms the code handed to execute() right now is
        # the SAME code that was hashed at submit() time, closing the gap between "what was
        # authorized" and "what is about to run."
        actual_digest = digest_of(intent.artifact_code)
        if not hmac.compare_digest(actual_digest, decision.artifact_digest):
            raise ArtifactMismatchError(
                f"artifact digest mismatch: decision authorized {decision.artifact_digest[:12]}..., "
                f"but the code about to run hashes to {actual_digest[:12]}..."
            )

        path = intent.payload["path"]
        if decision.execution_class == "same_process":
            namespace: dict = {"OUTPUT_PATH": path}
            exec(intent.artifact_code, namespace)  # noqa: S102 -- the whole point: run exactly the authorized code
            return {"execution_class": "same_process"}

        if decision.execution_class == "separate_process":
            proc = subprocess.run(
                [sys.executable, "-c", intent.artifact_code, path],
                capture_output=True, text=True, check=True,
            )
            return {"execution_class": "separate_process", "stdout": proc.stdout}

        raise GateViolation(f"unknown execution_class={decision.execution_class!r}")


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------

_PROGRAM_A = """
import sys
with open(sys.argv[1], "w") as f:
    f.write("written by program A")
"""

_PROGRAM_B = """
import sys
with open(sys.argv[1], "w") as f:
    f.write("written by program B -- this should never run under A's authorization")
"""


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-008-"))
    results: dict = {"workdir": str(workdir)}

    gate = Gate()
    executor = Executor(gate)

    # --- Predicate A: happy path -- authorized artifact runs, digest matches, effect confirmed. -
    a_path = workdir / "happy_path.txt"
    a_intent = Intent(
        kind="run_artifact", principal_id="principal-alice", intent_id=str(uuid.uuid4()),
        payload={"path": str(a_path)}, consequence="high", artifact_code=_PROGRAM_A,
    )
    a_decision = gate.submit(a_intent)
    a_effect = executor.execute(a_decision, a_intent)
    results["predicate_a_happy_path"] = {
        "execution_class": a_effect["execution_class"],
        "file_exists": a_path.exists(),
        "file_content_matches_program_a": a_path.exists() and a_path.read_text() == "written by program A",
        "digest_bound_in_decision": a_decision.artifact_digest == digest_of(_PROGRAM_A),
    }

    # --- Predicate B: swapped artifact -- authorized A, presented with B at execution time. -----
    b_path = workdir / "swapped.txt"
    b_real_intent = Intent(
        kind="run_artifact", principal_id="principal-alice", intent_id=str(uuid.uuid4()),
        payload={"path": str(b_path)}, consequence="high", artifact_code=_PROGRAM_A,
    )
    b_decision = gate.submit(b_real_intent)  # authorizes program A's digest
    b_swapped_intent = Intent(
        intent_id=b_real_intent.intent_id, principal_id=b_real_intent.principal_id, kind=b_real_intent.kind,
        payload=b_real_intent.payload, consequence=b_real_intent.consequence, artifact_code=_PROGRAM_B,
    )
    b_raised = None
    try:
        executor.execute(b_decision, b_swapped_intent)  # presents program B against A's Decision
    except ArtifactMismatchError as exc:
        b_raised = str(exc)
    results["predicate_b_swapped_artifact_refused"] = {
        "raised_artifact_mismatch": b_raised is not None,
        "error": b_raised,
        "file_absent": not b_path.exists(),
    }

    # --- Predicate C: forged Decision (never through Gate.submit()) refused before execution. ---
    c_path = workdir / "forged.txt"
    c_intent = Intent(
        kind="run_artifact", principal_id="principal-mallory", intent_id=str(uuid.uuid4()),
        payload={"path": str(c_path)}, consequence="high", artifact_code=_PROGRAM_A,
    )
    c_forged = Decision(
        intent_id=c_intent.intent_id, principal_id=c_intent.principal_id, kind=c_intent.kind,
        permitted=True, execution_class="separate_process", artifact_digest=digest_of(_PROGRAM_A),
        token="beadfeed" * 8,
    )
    c_refused = False
    try:
        executor.execute(c_forged, c_intent)
    except GateViolation:
        c_refused = True
    results["predicate_c_forged_refused"] = {"refused": c_refused, "file_absent": not c_path.exists()}

    # --- Predicate D: digest-tamper replay -- a genuine Decision, artifact_digest swapped to a --
    # DIFFERENT legitimate-looking digest (program B's real digest), token left unchanged. --------
    d_path = workdir / "digest_tamper.txt"
    d_intent = Intent(
        kind="run_artifact", principal_id="principal-bob", intent_id=str(uuid.uuid4()),
        payload={"path": str(d_path)}, consequence="high", artifact_code=_PROGRAM_A,
    )
    d_genuine = gate.submit(d_intent)
    d_tampered = Decision(
        intent_id=d_genuine.intent_id, principal_id=d_genuine.principal_id, kind=d_genuine.kind,
        permitted=d_genuine.permitted, execution_class=d_genuine.execution_class,
        artifact_digest=digest_of(_PROGRAM_B),  # swapped to B's real digest, not garbage
        token=d_genuine.token,  # token minted for A's digest, unchanged
    )
    d_verifies = gate.verify(d_tampered)
    d_refused = False
    try:
        executor.execute(d_tampered, d_intent)  # d_intent still carries program A's code
    except GateViolation:
        d_refused = True
    results["predicate_d_digest_tamper_refused"] = {
        "gate_verify_result": d_verifies,
        "executor_refused": d_refused,
        "file_absent": not d_path.exists(),
    }

    return results


def main() -> int:
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_happy_path"]
    b = results["predicate_b_swapped_artifact_refused"]
    c = results["predicate_c_forged_refused"]
    d = results["predicate_d_digest_tamper_refused"]

    checks = [
        ("happy path: file content matches program A", a["file_content_matches_program_a"] is True),
        ("happy path: digest bound in Decision", a["digest_bound_in_decision"] is True),
        ("swapped artifact: ArtifactMismatchError raised", b["raised_artifact_mismatch"] is True),
        ("swapped artifact: target file absent", b["file_absent"] is True),
        ("forged: refused", c["refused"] is True),
        ("forged: target file absent", c["file_absent"] is True),
        ("digest tamper: Gate.verify() returns False", d["gate_verify_result"] is False),
        ("digest tamper: Executor refuses", d["executor_refused"] is True),
        ("digest tamper: target file absent", d["file_absent"] is True),
    ]
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    shutil.rmtree(results["workdir"], ignore_errors=True)

    if not ok:
        print("HYPOTHESIS NOT SUPPORTED", file=sys.stderr)
        return 1
    print("HYPOTHESIS SUPPORTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
