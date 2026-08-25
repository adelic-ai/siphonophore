"""Reproducer for lab/001 -- can an effect happen except through the Gate.

The smallest possible proof of DESIGN.md section 1's central claim: one uniform mediation gate for
every effect-producing action, with no other path to the effect. This script defines the minimum
Intent/Decision/Gate/Executor shape needed to test that claim structurally, not just by convention
-- Executor.execute() requires a Decision, and a Decision can only be validly minted by
Gate.submit() (an HMAC only the Gate's own secret can produce). A hand-forged Decision is the
actual bypass attempt: not "some other code wrote a file," which proves nothing about this
system's own enforcement, but "can application code construct something Executor.execute() will
accept without having gone through Gate.submit() first."

Run:

    cd ~/dev/siphonophore
    python3 lab/001_gate_blocks_unmediated_effects.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "lab" / "out" / "001"
SANDBOX = OUT_DIR / "sandbox"


# ---------------------------------------------------------------------------
# The minimum Intent / Decision / Gate / Executor shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    principal_id: str
    kind: str
    payload: dict


@dataclass(frozen=True)
class Decision:
    """Deliberately carries a token, not just a bool. permitted=True alone would be trivially
    fake-able by anyone who can construct a dataclass -- the token is the actual unforgeable part,
    the same shape as the check-in nonce in the archived v1 primitives."""

    intent_id: str
    principal_id: str
    permitted: bool
    token: str


class Gate:
    """The only thing that mints a valid Decision. `_secret` never leaves this instance --
    Executor gets a bound verify() callback, not the secret itself, mirroring the credential-
    injection pattern (the caller gets a capability, never the underlying secret)."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def submit(self, intent: Intent) -> Decision:
        intent_id = str(uuid.uuid4())
        # Trivial policy for this first slice: file_write under SANDBOX is permitted, anything
        # else is not. A real Policy/Authority layer is explicitly future work (DESIGN.md section
        # 6) -- this is the smallest thing that lets the mediated-vs-unmediated distinction be
        # tested at all, not a real authorization engine.
        permitted = intent.kind == "file_write" and str(Path(intent.payload["path"]).resolve()).startswith(
            str(SANDBOX.resolve())
        )
        token = self._mint(intent_id, intent.principal_id, permitted)
        return Decision(intent_id=intent_id, principal_id=intent.principal_id, permitted=permitted, token=token)

    def _mint(self, intent_id: str, principal_id: str, permitted: bool) -> str:
        message = f"{intent_id}:{principal_id}:{permitted}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(decision.intent_id, decision.principal_id, decision.permitted)
        return hmac.compare_digest(expected, decision.token)


class GateBypassError(PermissionError):
    """Raised when Executor.execute() is called with a Decision that didn't actually come from a
    real Gate.submit() call -- the specific, structural proof this experiment is testing for."""


class Executor:
    """The only thing that performs the actual effect. Takes a Decision, never a raw Intent --
    there is no execute(path, content) overload that skips the Decision entirely."""

    def __init__(self, verify_fn) -> None:
        self._verify = verify_fn

    def execute(self, decision: Decision, payload: dict) -> Path:
        if not self._verify(decision):
            raise GateBypassError(f"decision {decision.intent_id!r} did not come from a real Gate -- effect refused")
        if not decision.permitted:
            raise PermissionError(f"intent {decision.intent_id!r} was not permitted by policy")
        path = Path(payload["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["content"])
        return path


# ---------------------------------------------------------------------------
# The experiment itself
# ---------------------------------------------------------------------------


def main() -> int:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)

    gate = Gate()
    executor = Executor(verify_fn=gate.verify)

    results: dict = {}

    # --- Attempt 1: the mediated path -----------------------------------------------------
    intent = Intent(
        principal_id="principal:test-user",
        kind="file_write",
        payload={"path": str(SANDBOX / "mediated.txt"), "content": "written through the gate"},
    )
    decision = gate.submit(intent)
    try:
        written_path = executor.execute(decision, intent.payload)
        on_disk = written_path.exists() and written_path.read_text() == "written through the gate"
        results["mediated_attempt"] = {
            "decision_permitted": decision.permitted,
            "executor_raised": None,
            "ground_truth_file_exists_with_expected_content": on_disk,
        }
    except Exception as exc:  # noqa: BLE001 -- this experiment wants to see any exception, not filter one
        results["mediated_attempt"] = {
            "decision_permitted": decision.permitted,
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists_with_expected_content": False,
        }

    # --- Attempt 2: the bypass -- a hand-forged Decision, never through Gate.submit() -----
    forged = Decision(
        intent_id=str(uuid.uuid4()),
        principal_id="principal:test-user",
        permitted=True,  # the attacker just claims permission -- no real Gate secret involved
        token="0" * 64,  # a guessed/empty token, not derived from the Gate's real secret
    )
    forged_payload = {"path": str(SANDBOX / "bypass.txt"), "content": "written WITHOUT the gate"}
    try:
        executor.execute(forged, forged_payload)
        bypass_path = Path(forged_payload["path"])
        results["bypass_attempt"] = {
            "executor_raised": None,
            "ground_truth_file_exists": bypass_path.exists(),
        }
    except Exception as exc:  # noqa: BLE001
        results["bypass_attempt"] = {
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists": Path(forged_payload["path"]).exists(),
        }

    # --- Write results -------------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results.json"
    results_path.write_text(json.dumps(results, indent=2))

    print(json.dumps(results, indent=2))
    print(f"\nResults written to {results_path}")

    mediated_ok = results["mediated_attempt"]["ground_truth_file_exists_with_expected_content"] is True
    bypass_blocked = (
        results["bypass_attempt"]["executor_raised"] is not None
        and results["bypass_attempt"]["ground_truth_file_exists"] is False
    )

    print(f"\nMediated write succeeded with ground-truth confirmation: {mediated_ok}")
    print(f"Bypass attempt was blocked (raised, no file written): {bypass_blocked}")
    print(f"\nHYPOTHESIS {'SUPPORTED' if (mediated_ok and bypass_blocked) else 'FALSIFIED'}")

    return 0 if (mediated_ok and bypass_blocked) else 1


if __name__ == "__main__":
    sys.exit(main())
