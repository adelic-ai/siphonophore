"""Reproducer for lab/003 -- is execution class a real, enforced decision, or just a label.

DESIGN.md section 2: execution class follows required authority/consequence, not capability type
(tool vs. agent). This experiment adds a `consequence` field to Intent, has Gate._policy map
consequence -> execution_class ("same_process" | "separate_process"), and tests whether that
actually changes *where* the effect runs -- not just whether a string gets attached to the
Decision that nothing downstream consults.

Following lab/002's lesson directly: adding a new bound field (execution_class) creates a new
replay surface the moment there's more than one value for it, the same way adding `kind` did in
002. So this experiment tests four things, not three:

1. A "low" consequence intent is dispatched same_process -- ground truth: the pid that actually
   performed the write matches this process's own pid.
2. A "high" consequence intent is dispatched separate_process -- ground truth: the pid that
   performed the write is a REAL, different OS pid (a real subprocess, not a same-process fake).
3. A forged Decision (never through Gate.submit()) is refused, same shape as 001/002.
4. NEW: a genuinely-minted "same_process" Decision cannot be relabeled "separate_process" (or vice
   versa) and reused -- the execution_class itself is now bound into the HMAC, specifically so
   this is testable and refused, applying 002's actual lesson rather than just repeating it in
   prose.

Run:

    cd ~/dev/siphonophore
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
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "lab" / "out" / "003"
SANDBOX = OUT_DIR / "sandbox"


# ---------------------------------------------------------------------------
# Intent / Decision / Gate / Executor -- evolved from lab/002
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    principal_id: str
    kind: str
    consequence: str  # "low" | "high" -- the signal DESIGN.md section 2 says should drive execution class
    payload: dict


@dataclass(frozen=True)
class Decision:
    """`execution_class` is new since lab/002, and -- same discipline as lab/002's `kind` binding
    -- it's bound into the HMAC, not carried alongside it unbound."""

    intent_id: str
    principal_id: str
    kind: str
    execution_class: str  # "same_process" | "separate_process"
    permitted: bool
    token: str


class Gate:
    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def submit(self, intent: Intent) -> Decision:
        intent_id = str(uuid.uuid4())
        permitted = self._policy(intent)
        execution_class = self._select_execution_class(intent)
        token = self._mint(intent_id, intent.principal_id, intent.kind, execution_class, permitted)
        return Decision(
            intent_id=intent_id,
            principal_id=intent.principal_id,
            kind=intent.kind,
            execution_class=execution_class,
            permitted=permitted,
            token=token,
        )

    def _policy(self, intent: Intent) -> bool:
        if intent.kind == "file_write":
            return str(Path(intent.payload["path"]).resolve()).startswith(str(SANDBOX.resolve()))
        return False

    def _select_execution_class(self, intent: Intent) -> str:
        """The actual DESIGN.md section 2 claim, operationalized: execution class follows
        required consequence, not what the caller happened to name the intent. Real deployment
        would also weigh untrusted-input exposure (DESIGN.md's other axis) -- consequence alone is
        enough to test whether the mechanism works at all, which is this experiment's job."""
        return "separate_process" if intent.consequence == "high" else "same_process"

    def _mint(self, intent_id: str, principal_id: str, kind: str, execution_class: str, permitted: bool) -> str:
        message = f"{intent_id}:{principal_id}:{kind}:{execution_class}:{permitted}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id, decision.principal_id, decision.kind, decision.execution_class, decision.permitted
        )
        return hmac.compare_digest(expected, decision.token)


class GateBypassError(PermissionError):
    pass


class Executor:
    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def execute(self, decision: Decision, payload: dict) -> dict:
        if not self._gate.verify(decision):
            raise GateBypassError(f"decision {decision.intent_id!r} did not come from a real Gate -- effect refused")
        if not decision.permitted:
            raise PermissionError(f"intent {decision.intent_id!r} was not permitted by policy")

        if decision.execution_class == "same_process":
            return self._write_same_process(payload)
        if decision.execution_class == "separate_process":
            return self._write_separate_process(payload)
        raise ValueError(f"unknown execution_class {decision.execution_class!r}")

    def _write_same_process(self, payload: dict) -> dict:
        path = Path(payload["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["content"])
        return {"path": str(path), "pid": os.getpid()}

    def _write_separate_process(self, payload: dict) -> dict:
        """A REAL subprocess, not a same-process function pretending to be one -- it reports its
        own os.getpid() from inside itself, which is the actual ground truth this experiment
        checks, not the parent's assumption about what pid a subprocess.run call must have used."""
        script = (
            "import json, os, pathlib\n"
            f"p = pathlib.Path({payload['path']!r})\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            f"p.write_text({payload['content']!r})\n"
            "print(json.dumps({'path': str(p), 'pid': os.getpid()}))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# The experiment itself
# ---------------------------------------------------------------------------


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)

    gate = Gate()
    executor = Executor(gate)
    results: dict = {}
    this_pid = os.getpid()

    # --- Attempt 1: low consequence -> same_process -----------------------------------------
    low_intent = Intent(
        principal_id="principal:test",
        kind="file_write",
        consequence="low",
        payload={"path": str(SANDBOX / "low.txt"), "content": "low consequence, same process"},
    )
    low_decision = gate.submit(low_intent)
    low_outcome = executor.execute(low_decision, low_intent.payload)
    results["low_consequence"] = {
        "selected_execution_class": low_decision.execution_class,
        "reported_pid": low_outcome["pid"],
        "ran_in_this_process": low_outcome["pid"] == this_pid,
        "ground_truth_file_content_correct": Path(low_outcome["path"]).read_text() == "low consequence, same process",
    }

    # --- Attempt 2: high consequence -> separate_process -------------------------------------
    high_intent = Intent(
        principal_id="principal:test",
        kind="file_write",
        consequence="high",
        payload={"path": str(SANDBOX / "high.txt"), "content": "high consequence, separate process"},
    )
    high_decision = gate.submit(high_intent)
    high_outcome = executor.execute(high_decision, high_intent.payload)
    results["high_consequence"] = {
        "selected_execution_class": high_decision.execution_class,
        "reported_pid": high_outcome["pid"],
        "ran_in_a_different_real_process": high_outcome["pid"] != this_pid,
        "ground_truth_file_content_correct": (
            Path(high_outcome["path"]).read_text() == "high consequence, separate process"
        ),
    }

    # --- Attempt 3: forged Decision -----------------------------------------------------------
    forged = Decision(
        intent_id=str(uuid.uuid4()),
        principal_id="principal:test",
        kind="file_write",
        execution_class="same_process",
        permitted=True,
        token="0" * 64,
    )
    forged_payload = {"path": str(SANDBOX / "forged.txt"), "content": "should not exist"}
    try:
        executor.execute(forged, forged_payload)
        results["forged_bypass"] = {"executor_raised": None, "ground_truth_file_exists": Path(forged_payload["path"]).exists()}
    except Exception as exc:  # noqa: BLE001
        results["forged_bypass"] = {
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists": Path(forged_payload["path"]).exists(),
        }

    # --- Attempt 4: cross-execution-class replay ----------------------------------------------
    # A REAL, legitimately-minted same_process Decision, relabeled separate_process and reused --
    # the interesting version of this bypass, since separate_process is meant to be the STRONGER
    # boundary; escaping FROM a weaker grant INTO the stronger one isn't the attack that matters
    # here (a real deployment would presumably not mind extra isolation) -- the one that matters is
    # relabeling a separate_process grant DOWN to same_process, dodging the isolation it was
    # actually given. Testing that direction specifically.
    real_high_intent = Intent(
        principal_id="principal:test",
        kind="file_write",
        consequence="high",
        payload={"path": str(SANDBOX / "legit-high.txt"), "content": "genuinely high consequence"},
    )
    real_high_decision = gate.submit(real_high_intent)  # genuinely minted as separate_process
    downgraded = Decision(
        intent_id=real_high_decision.intent_id,
        principal_id=real_high_decision.principal_id,
        kind=real_high_decision.kind,
        execution_class="same_process",  # relabeled DOWN -- dodging the isolation it was actually granted
        permitted=real_high_decision.permitted,
        token=real_high_decision.token,  # the same real token, unmodified
    )
    replay_payload = {"path": str(SANDBOX / "downgraded-replay.txt"), "content": "should not exist"}
    try:
        outcome = executor.execute(downgraded, replay_payload)
        results["execution_class_downgrade_replay"] = {
            "executor_raised": None,
            "ran_in_this_process": outcome.get("pid") == this_pid,
            "ground_truth_file_exists": Path(replay_payload["path"]).exists(),
        }
    except Exception as exc:  # noqa: BLE001
        results["execution_class_downgrade_replay"] = {
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists": Path(replay_payload["path"]).exists(),
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results.json"
    results_path.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\nResults written to {results_path}")

    low_ok = results["low_consequence"]["ran_in_this_process"] and results["low_consequence"]["ground_truth_file_content_correct"]
    high_ok = (
        results["high_consequence"]["ran_in_a_different_real_process"]
        and results["high_consequence"]["ground_truth_file_content_correct"]
    )
    forged_blocked = (
        results["forged_bypass"]["executor_raised"] is not None and not results["forged_bypass"]["ground_truth_file_exists"]
    )
    downgrade_blocked = (
        results["execution_class_downgrade_replay"]["executor_raised"] is not None
        and not results["execution_class_downgrade_replay"]["ground_truth_file_exists"]
    )

    print(f"\nLow-consequence intent ran same-process, correct content: {low_ok}")
    print(f"High-consequence intent ran in a real, different process, correct content: {high_ok}")
    print(f"Forged Decision was blocked: {forged_blocked}")
    print(f"Execution-class downgrade replay was blocked: {downgrade_blocked}")
    all_pass = low_ok and high_ok and forged_blocked and downgrade_blocked
    print(f"\nHYPOTHESIS {'SUPPORTED' if all_pass else 'FALSIFIED'}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
