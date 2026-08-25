"""Reproducer for lab/002 -- does delegation reduce to the same primitive as a tool call.

Extends lab/001's Intent/Decision/Gate/Executor shape with a second intent kind, "delegate", and
tests three things instead of two:

1. A mediated delegation succeeds, and the delegated sub-agent's OWN effect (a file write it
   performs) is independently confirmed on disk -- proving the sub-agent's action itself went
   through the same Gate, not a shortcut opened by the parent's delegation.
2. A hand-forged "delegate" Decision (never through Gate.submit()) is refused, same shape as
   lab/001's bypass proof, now for the second intent kind.
3. NEW, not testable with only one intent kind: a legitimately-minted Decision for kind="file_write"
   cannot be replayed to authorize a kind="delegate" effect, or vice versa. lab/001's Decision only
   bound intent_id:principal_id:permitted -- with two kinds, does the same real Decision object
   authorize something other than what it was actually minted for? Decision gains a `kind` field
   here, and the HMAC now binds it, specifically so this replay is testable and refused.

Run:

    cd ~/dev/siphonophore
    python3 lab/002_delegation_through_the_same_gate.py
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
OUT_DIR = REPO_ROOT / "lab" / "out" / "002"
SANDBOX = OUT_DIR / "sandbox"


# ---------------------------------------------------------------------------
# Intent / Decision / Gate / Executor -- evolved from lab/001
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    principal_id: str
    kind: str  # "file_write" | "delegate"
    payload: dict


@dataclass(frozen=True)
class Decision:
    """`kind` is new since lab/001, and -- the actual point of this experiment -- it's bound into
    the HMAC, not just carried alongside it. A Decision that didn't bind kind would let a real
    file_write authorization be replayed as a delegate authorization (or vice versa), since
    Executor would have no way to tell the token was ever meant for a different effect."""

    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    token: str


class Gate:
    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def submit(self, intent: Intent) -> Decision:
        intent_id = str(uuid.uuid4())
        permitted = self._policy(intent)
        token = self._mint(intent_id, intent.principal_id, intent.kind, permitted)
        return Decision(
            intent_id=intent_id, principal_id=intent.principal_id, kind=intent.kind, permitted=permitted, token=token
        )

    def _policy(self, intent: Intent) -> bool:
        """Still trivial -- a real Policy/Authority layer is future work (DESIGN.md section 6).
        file_write is permitted under SANDBOX; delegate is permitted unconditionally for this
        experiment (any real deployment would check the delegate target's own scope here)."""
        if intent.kind == "file_write":
            return str(Path(intent.payload["path"]).resolve()).startswith(str(SANDBOX.resolve()))
        if intent.kind == "delegate":
            return True
        return False

    def _mint(self, intent_id: str, principal_id: str, kind: str, permitted: bool) -> str:
        message = f"{intent_id}:{principal_id}:{kind}:{permitted}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(decision.intent_id, decision.principal_id, decision.kind, decision.permitted)
        return hmac.compare_digest(expected, decision.token)


class GateBypassError(PermissionError):
    pass


class Executor:
    """Delegation doesn't get its own shortcut: executing a "delegate" Decision constructs a NEW
    Intent for the sub-agent's actual action and submits IT through the same Gate this Executor
    was already given -- there is no code path where a sub-agent's effect happens without also
    going through Gate.submit()."""

    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def execute(self, decision: Decision, payload: dict) -> Path:
        if not self._gate.verify(decision):
            raise GateBypassError(f"decision {decision.intent_id!r} did not come from a real Gate -- effect refused")
        if not decision.permitted:
            raise PermissionError(f"intent {decision.intent_id!r} was not permitted by policy")

        if decision.kind == "file_write":
            path = Path(payload["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload["content"])
            return path

        if decision.kind == "delegate":
            sub_intent = Intent(
                principal_id=f"{decision.principal_id}/sub-agent",
                kind="file_write",
                payload=payload["sub_payload"],
            )
            sub_decision = self._gate.submit(sub_intent)  # the sub-agent's own mediation, not skipped
            return self.execute(sub_decision, sub_intent.payload)

        raise ValueError(f"unknown intent kind {decision.kind!r}")


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

    # --- Attempt 1: mediated delegation -----------------------------------------------------
    delegate_intent = Intent(
        principal_id="principal:parent-agent",
        kind="delegate",
        payload={
            "sub_payload": {
                "path": str(SANDBOX / "delegated.txt"),
                "content": "written by the delegated sub-agent, through the same gate",
            }
        },
    )
    decision = gate.submit(delegate_intent)
    try:
        written_path = executor.execute(decision, delegate_intent.payload)
        on_disk = written_path.exists() and written_path.read_text() == (
            "written by the delegated sub-agent, through the same gate"
        )
        results["mediated_delegation"] = {
            "decision_permitted": decision.permitted,
            "executor_raised": None,
            "ground_truth_subagent_file_exists_with_expected_content": on_disk,
        }
    except Exception as exc:  # noqa: BLE001
        results["mediated_delegation"] = {
            "decision_permitted": decision.permitted,
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_subagent_file_exists_with_expected_content": False,
        }

    # --- Attempt 2: forged delegate Decision, never through Gate.submit() -------------------
    forged = Decision(
        intent_id=str(uuid.uuid4()),
        principal_id="principal:parent-agent",
        kind="delegate",
        permitted=True,
        token="0" * 64,
    )
    forged_payload = {"sub_payload": {"path": str(SANDBOX / "forged-delegate.txt"), "content": "should not exist"}}
    try:
        executor.execute(forged, forged_payload)
        bypass_path = Path(forged_payload["sub_payload"]["path"])
        results["forged_delegate_bypass"] = {"executor_raised": None, "ground_truth_file_exists": bypass_path.exists()}
    except Exception as exc:  # noqa: BLE001
        results["forged_delegate_bypass"] = {
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists": Path(forged_payload["sub_payload"]["path"]).exists(),
        }

    # --- Attempt 3: cross-kind replay -- a REAL file_write Decision, reused to authorize -----
    # a delegate effect it was never minted for.
    real_write_intent = Intent(
        principal_id="principal:parent-agent",
        kind="file_write",
        payload={"path": str(SANDBOX / "legit-write.txt"), "content": "a real, legitimately mediated write"},
    )
    real_write_decision = gate.submit(real_write_intent)  # genuinely minted -- verify() will pass on its own kind
    replayed = Decision(
        intent_id=real_write_decision.intent_id,
        principal_id=real_write_decision.principal_id,
        kind="delegate",  # relabeled -- everything else about this token is real
        permitted=real_write_decision.permitted,
        token=real_write_decision.token,
    )
    replay_payload = {"sub_payload": {"path": str(SANDBOX / "replayed-delegate.txt"), "content": "should not exist"}}
    try:
        executor.execute(replayed, replay_payload)
        replay_path = Path(replay_payload["sub_payload"]["path"])
        results["cross_kind_replay"] = {"executor_raised": None, "ground_truth_file_exists": replay_path.exists()}
    except Exception as exc:  # noqa: BLE001
        results["cross_kind_replay"] = {
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists": Path(replay_payload["sub_payload"]["path"]).exists(),
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results.json"
    results_path.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\nResults written to {results_path}")

    delegation_ok = results["mediated_delegation"]["ground_truth_subagent_file_exists_with_expected_content"] is True
    forged_blocked = (
        results["forged_delegate_bypass"]["executor_raised"] is not None
        and results["forged_delegate_bypass"]["ground_truth_file_exists"] is False
    )
    replay_blocked = (
        results["cross_kind_replay"]["executor_raised"] is not None
        and results["cross_kind_replay"]["ground_truth_file_exists"] is False
    )

    print(f"\nMediated delegation succeeded with ground-truth confirmation: {delegation_ok}")
    print(f"Forged delegate Decision was blocked: {forged_blocked}")
    print(f"Cross-kind replay (real file_write token reused as delegate) was blocked: {replay_blocked}")
    all_pass = delegation_ok and forged_blocked and replay_blocked
    print(f"\nHYPOTHESIS {'SUPPORTED' if all_pass else 'FALSIFIED'}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
