"""003 -- Execution class selection.

See `lab/003-execution-class-selection.md` for the hypothesis (with explicit
null), method, and analysis. This script runs the experiment and writes
results under `out/003/`.

Adds `consequence` on `Intent` and `execution_class` (`same_process` |
`separate_process`) on `Decision`, with `Gate` mapping consequence to class
(DESIGN.md SS2). Proves with real, distinct process IDs -- self-reported by
a real `subprocess.run`-spawned process, not assumed by the parent -- that
execution class actually changes *where* the effect runs, not just what
it's labeled.

Same discipline as 002: `execution_class` is bound into the HMAC from the
first line of this file's Gate implementation, and a real test proves a
genuinely-minted `separate_process` Decision cannot be downgrade-replayed to
`same_process`. Built fresh; stdlib only, no import from any other file in
this repo or elsewhere (DESIGN.md SS0).

Run::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/003_execution_class_selection.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "003"


# ---------------------------------------------------------------------------
# Core primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    kind: str  # "write_file"
    principal_id: str
    intent_id: str
    payload: dict
    consequence: str  # "low" | "high" -- drives execution class selection


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str  # "same_process" | "separate_process"
    token: str


class Gate:
    """Maps consequence -> execution class as policy, and binds
    execution_class into the HMAC alongside every other field Executor
    branches on -- from the first line, per DESIGN.md SS2's standing rule
    (found empirically in 002/HISTORY.md, applied here from the start)."""

    CONSEQUENCE_TO_CLASS = {
        "low": "same_process",
        "high": "separate_process",
    }

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str
    ) -> bytes:
        # execution_class bound in from the start -- this is the field this
        # experiment adds, and the standing rule says every dispatch-relevant
        # field must be here the moment it's introduced, no exceptions.
        return f"{intent_id}:{principal_id}:{kind}:{permitted}:{execution_class}".encode("utf-8")

    def _mint(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str
    ) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted, execution_class)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        permitted = self._policy(intent)
        execution_class = self.CONSEQUENCE_TO_CLASS.get(intent.consequence, "same_process")
        token = self._mint(intent.intent_id, intent.principal_id, intent.kind, permitted, execution_class)
        return Decision(
            intent_id=intent.intent_id,
            principal_id=intent.principal_id,
            kind=intent.kind,
            permitted=permitted,
            execution_class=execution_class,
            token=token,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id,
            decision.principal_id,
            decision.kind,
            decision.permitted,
            decision.execution_class,
        )
        return hmac.compare_digest(expected, decision.token)

    def _policy(self, intent: Intent) -> bool:
        return intent.kind in ("write_file",) and intent.consequence in ("low", "high")


class GateViolation(PermissionError):
    pass


# A tiny, self-contained child-process program: writes a file and reports
# its OWN pid via stdout JSON. The parent never assumes the child's pid --
# it's read from this real subprocess's own stdout after it exits.
_CHILD_PROGRAM = """
import json, os, sys
path, content = sys.argv[1], sys.argv[2]
with open(path, "w") as f:
    f.write(content)
print(json.dumps({"pid": os.getpid(), "path": path}))
"""


class Executor:
    """Branches on decision.execution_class to decide WHERE the effect
    runs, not merely how it's labeled."""

    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def execute(self, decision: Decision, intent: Intent) -> dict:
        if decision.intent_id != intent.intent_id or decision.kind != intent.kind:
            raise GateViolation("decision does not correspond to this intent")
        if not self._gate.verify(decision):
            raise GateViolation("decision failed Gate verification -- forged, tampered, or downgraded")
        if not decision.permitted:
            raise GateViolation("decision denies this intent")

        if intent.kind != "write_file":
            raise GateViolation(f"no executor handler for kind={intent.kind!r}")

        path = intent.payload["path"]
        content = intent.payload["content"]

        if decision.execution_class == "same_process":
            with open(path, "w") as f:
                f.write(content)
            acting_pid = os.getpid()  # this process performed the effect
            return {
                "effect": "write_file",
                "path": path,
                "execution_class": "same_process",
                "acting_pid": acting_pid,
            }

        if decision.execution_class == "separate_process":
            proc = subprocess.run(
                [sys.executable, "-c", _CHILD_PROGRAM, path, content],
                capture_output=True,
                text=True,
                check=True,
            )
            child_report = json.loads(proc.stdout.strip())
            return {
                "effect": "write_file",
                "path": path,
                "execution_class": "separate_process",
                "acting_pid": child_report["pid"],  # self-reported by the real spawned process
            }

        raise GateViolation(f"unknown execution_class={decision.execution_class!r}")


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-003-"))
    results: dict = {"workdir": str(workdir), "executor_process_pid": os.getpid()}

    gate = Gate()
    executor = Executor(gate)

    # --- Predicate A: consequence="low" maps to same_process, and the
    # acting pid genuinely equals the Executor's own process pid. ----------
    low_path = workdir / "low_consequence.txt"
    low_intent = Intent(
        kind="write_file",
        principal_id="principal-alice",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(low_path), "content": "low-consequence effect"},
        consequence="low",
    )
    low_decision = gate.submit(low_intent)
    low_effect = executor.execute(low_decision, low_intent)
    results["predicate_a_same_process"] = {
        "execution_class_assigned": low_decision.execution_class,
        "effect": low_effect,
        "file_exists": low_path.exists(),
        "file_content_matches": low_path.exists() and low_path.read_text() == "low-consequence effect",
        "acting_pid_equals_executor_pid": low_effect["acting_pid"] == os.getpid(),
    }

    # --- Predicate B: consequence="high" maps to separate_process, and the
    # acting pid -- self-reported by a real spawned subprocess -- genuinely
    # differs from the Executor's own pid. ----------------------------------
    high_path = workdir / "high_consequence.txt"
    high_intent = Intent(
        kind="write_file",
        principal_id="principal-alice",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(high_path), "content": "high-consequence effect"},
        consequence="high",
    )
    high_decision = gate.submit(high_intent)
    high_effect = executor.execute(high_decision, high_intent)
    results["predicate_b_separate_process"] = {
        "execution_class_assigned": high_decision.execution_class,
        "effect": high_effect,
        "file_exists": high_path.exists(),
        "file_content_matches": high_path.exists() and high_path.read_text() == "high-consequence effect",
        "acting_pid_differs_from_executor_pid": high_effect["acting_pid"] != os.getpid(),
    }

    # --- Predicate C: forged Decision (arbitrary token, execution_class
    # claimed as separate_process) is refused, no file written. ------------
    forged_path = workdir / "forged.txt"
    forged_intent = Intent(
        kind="write_file",
        principal_id="principal-mallory",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(forged_path), "content": "should never appear"},
        consequence="high",
    )
    forged_decision = Decision(
        intent_id=forged_intent.intent_id,
        principal_id=forged_intent.principal_id,
        kind=forged_intent.kind,
        permitted=True,
        execution_class="separate_process",
        token="cafebabe" * 8,
    )
    forged_refused = False
    try:
        executor.execute(forged_decision, forged_intent)
    except GateViolation:
        forged_refused = True
    results["predicate_c_forged_refused"] = {
        "refused": forged_refused,
        "forged_file_absent": not forged_path.exists(),
    }

    # --- Predicate D: downgrade-replay. Take a genuinely-minted
    # separate_process Decision and construct a new Decision with
    # execution_class flipped to same_process, token left byte-for-byte
    # unchanged. Must fail Gate.verify() and be refused by Executor. -------
    downgrade_path = workdir / "downgrade.txt"
    downgrade_intent = Intent(
        kind="write_file",
        principal_id="principal-bob",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(downgrade_path), "content": "should never appear via downgrade"},
        consequence="high",
    )
    genuine_high_decision = gate.submit(downgrade_intent)
    assert genuine_high_decision.execution_class == "separate_process"  # sanity

    downgraded_decision = Decision(
        intent_id=genuine_high_decision.intent_id,
        principal_id=genuine_high_decision.principal_id,
        kind=genuine_high_decision.kind,
        permitted=genuine_high_decision.permitted,
        execution_class="same_process",  # <-- downgraded, everything else identical
        token=genuine_high_decision.token,  # <-- token minted for separate_process, unchanged
    )
    downgrade_verifies = gate.verify(downgraded_decision)
    downgrade_refused = False
    try:
        executor.execute(downgraded_decision, downgrade_intent)
    except GateViolation:
        downgrade_refused = True
    results["predicate_d_downgrade_replay_refused"] = {
        "gate_verify_result": downgrade_verifies,
        "executor_refused": downgrade_refused,
        "downgrade_target_file_absent": not downgrade_path.exists(),
    }

    return results


def main() -> int:
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_same_process"]
    b = results["predicate_b_separate_process"]
    c = results["predicate_c_forged_refused"]
    d = results["predicate_d_downgrade_replay_refused"]

    checks = [
        ("low consequence -> same_process assigned", a["execution_class_assigned"] == "same_process"),
        ("same_process file content matches", a["file_content_matches"] is True),
        ("same_process acting pid == executor pid", a["acting_pid_equals_executor_pid"] is True),
        ("high consequence -> separate_process assigned", b["execution_class_assigned"] == "separate_process"),
        ("separate_process file content matches", b["file_content_matches"] is True),
        ("separate_process acting pid != executor pid", b["acting_pid_differs_from_executor_pid"] is True),
        ("forged decision refused", c["refused"] is True),
        ("forged file absent", c["forged_file_absent"] is True),
        ("downgrade: Gate.verify() returns False", d["gate_verify_result"] is False),
        ("downgrade: Executor refuses", d["executor_refused"] is True),
        ("downgrade target file absent", d["downgrade_target_file_absent"] is True),
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
