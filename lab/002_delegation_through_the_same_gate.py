"""002 -- Delegation through the same gate.

See `lab/002-delegation-through-the-same-gate.md` for the hypothesis (with
explicit null), method, and analysis. This script runs the experiment and
writes results under `out/002/`.

Adds `delegate` as a second `Intent.kind`, through the identical
Gate/Executor pipeline built in 001. `Executor.execute()` on a delegate
Decision constructs a *new* Intent for the sub-agent's own action and
submits it through the same Gate -- no shortcut, no direct dispatch of the
sub-agent's effect. This is the structural claim DESIGN.md SS7 makes:
delegation reduces to the exact same primitive a tool call does.

Built fresh for this experiment (stdlib only, no imports outside this file
-- DESIGN.md SS0). `Decision.kind` is bound into the HMAC from the first
line of Gate-minting code below, per HISTORY.md's account of finding that
gap only after building it once already -- this time it's there from the
start, and this script still writes a real test proving it rather than
taking it on faith.

Run::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/002_delegation_through_the_same_gate.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "002"


# ---------------------------------------------------------------------------
# Core primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    kind: str  # "write_file" | "delegate"
    principal_id: str
    intent_id: str
    payload: dict


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    token: str


class Gate:
    """Single mediation authority for both kinds. The secret never leaves
    this instance -- Executor calls gate.verify(), never reads the secret."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(self, intent_id: str, principal_id: str, kind: str, permitted: bool) -> bytes:
        # `kind` bound in from line one -- this is the field 001/HISTORY.md
        # found missing only after the fact, last time this was built.
        return f"{intent_id}:{principal_id}:{kind}:{permitted}".encode("utf-8")

    def _mint(self, intent_id: str, principal_id: str, kind: str, permitted: bool) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        permitted = self._policy(intent)
        token = self._mint(intent.intent_id, intent.principal_id, intent.kind, permitted)
        return Decision(
            intent_id=intent.intent_id,
            principal_id=intent.principal_id,
            kind=intent.kind,
            permitted=permitted,
            token=token,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(decision.intent_id, decision.principal_id, decision.kind, decision.permitted)
        return hmac.compare_digest(expected, decision.token)

    def _policy(self, intent: Intent) -> bool:
        return intent.kind in ("write_file", "delegate")


class GateViolation(PermissionError):
    pass


class Executor:
    """Handles both kinds. Crucially: the `delegate` handler does not
    perform the sub-agent's effect itself. It builds a brand-new Intent
    (fresh intent_id, sub-agent's own principal_id) and pushes it back
    through `self._gate.submit()` -> `self.execute()` -- the identical path
    a top-level caller would use for a `write_file` intent."""

    def __init__(self, gate: Gate) -> None:
        self._gate = gate

    def execute(self, decision: Decision, intent: Intent) -> dict:
        if decision.intent_id != intent.intent_id or decision.kind != intent.kind:
            raise GateViolation("decision does not correspond to this intent")
        if not self._gate.verify(decision):
            raise GateViolation("decision failed Gate verification -- forged or tampered")
        if not decision.permitted:
            raise GateViolation("decision denies this intent")

        if intent.kind == "write_file":
            path = Path(intent.payload["path"])
            path.write_text(intent.payload["content"])
            return {"effect": "write_file", "path": str(path), "principal_id": intent.principal_id}

        if intent.kind == "delegate":
            # No shortcut: construct a *new* Intent for the sub-agent's own
            # action and submit it through the same Gate. The sub-agent's
            # effect is only ever produced via a second, independent trip
            # through Gate.submit() -> Executor.execute() -- never by this
            # branch performing the effect directly on the delegator's
            # behalf.
            sub_spec = intent.payload["sub_intent"]
            sub_intent = Intent(
                kind=sub_spec["kind"],
                principal_id=sub_spec["principal_id"],  # sub-agent's OWN principal
                intent_id=str(uuid.uuid4()),  # freshly minted, not reused
                payload=sub_spec["payload"],
            )
            sub_decision = self._gate.submit(sub_intent)
            sub_effect = self.execute(sub_decision, sub_intent)
            return {
                "effect": "delegate",
                "delegator_principal_id": intent.principal_id,
                "sub_intent_id": sub_intent.intent_id,
                "sub_effect": sub_effect,
            }

        raise GateViolation(f"no executor handler for kind={intent.kind!r}")


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-002-"))
    results: dict = {"workdir": str(workdir)}

    gate = Gate()
    executor = Executor(gate)

    # --- Predicate A: mediated delegation succeeds, with the sub-agent's
    # OWN effect confirmed on disk, attributed to the sub-agent's own
    # principal_id (not the delegator's). ----------------------------------
    sub_path = workdir / "sub_agent_effect.txt"
    sub_content = f"written by the sub-agent, nonce={uuid.uuid4().hex}"
    delegate_intent = Intent(
        kind="delegate",
        principal_id="principal-alice",
        intent_id=str(uuid.uuid4()),
        payload={
            "sub_intent": {
                "kind": "write_file",
                "principal_id": "principal-alice.sub-agent-1",
                "payload": {"path": str(sub_path), "content": sub_content},
            }
        },
    )
    delegate_decision = gate.submit(delegate_intent)
    delegate_effect = executor.execute(delegate_decision, delegate_intent)

    sub_file_exists = sub_path.exists()
    sub_file_content_matches = sub_file_exists and sub_path.read_text() == sub_content
    sub_effect_attributed_correctly = (
        delegate_effect["sub_effect"]["principal_id"] == "principal-alice.sub-agent-1"
    )
    results["predicate_a_mediated_delegation"] = {
        "delegate_decision_permitted": delegate_decision.permitted,
        "delegate_effect": delegate_effect,
        "sub_file_exists": sub_file_exists,
        "sub_file_content_matches": sub_file_content_matches,
        "sub_effect_attributed_to_subagent": sub_effect_attributed_correctly,
    }

    # --- Predicate B: a forged delegate Decision is refused, and no
    # sub-agent effect is produced. -----------------------------------------
    forged_sub_path = workdir / "forged_sub_effect.txt"
    forged_delegate_intent = Intent(
        kind="delegate",
        principal_id="principal-mallory",
        intent_id=str(uuid.uuid4()),
        payload={
            "sub_intent": {
                "kind": "write_file",
                "principal_id": "principal-mallory.sub-agent-1",
                "payload": {"path": str(forged_sub_path), "content": "should never appear"},
            }
        },
    )
    forged_decision = Decision(
        intent_id=forged_delegate_intent.intent_id,
        principal_id=forged_delegate_intent.principal_id,
        kind=forged_delegate_intent.kind,
        permitted=True,
        token="deadbeef" * 8,
    )
    refused = False
    try:
        executor.execute(forged_decision, forged_delegate_intent)
    except GateViolation:
        refused = True
    results["predicate_b_forged_delegate_refused"] = {
        "refused": refused,
        "forged_sub_file_absent": not forged_sub_path.exists(),
    }

    # --- Predicate C: a genuinely-minted Decision for one kind cannot be
    # relabeled and replayed to authorize the other kind. Take a real,
    # validly-minted `write_file` Decision and a real, validly-minted
    # `delegate` Decision, swap their `kind` fields, and confirm both
    # relabeled objects fail Gate.verify() and are refused by Executor. ---
    write_path = workdir / "kind_binding_write.txt"
    write_intent = Intent(
        kind="write_file",
        principal_id="principal-bob",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(write_path), "content": "real write_file effect"},
    )
    write_decision = gate.submit(write_intent)
    assert write_decision.permitted  # sanity: this is a real, valid Decision

    relabel_path = workdir / "kind_binding_relabeled.txt"
    delegate_intent_2 = Intent(
        kind="delegate",
        principal_id="principal-bob",
        intent_id=str(uuid.uuid4()),
        payload={
            "sub_intent": {
                "kind": "write_file",
                "principal_id": "principal-bob.sub-agent-1",
                "payload": {"path": str(relabel_path), "content": "should never appear via relabel"},
            }
        },
    )
    delegate_decision_2 = gate.submit(delegate_intent_2)
    assert delegate_decision_2.permitted  # sanity: also a real, valid Decision

    # Relabel: take the genuinely-minted write_file Decision's token and
    # attach it to a `kind="delegate"` Decision for a DIFFERENT intent_id
    # that a delegate submission actually produced -- i.e., attempt to
    # authorize the delegate_intent_2 submission using write_decision's
    # token verbatim (same token bytes, reused for a different kind/intent).
    relabeled_decision = Decision(
        intent_id=delegate_intent_2.intent_id,
        principal_id=delegate_intent_2.principal_id,
        kind="delegate",
        permitted=True,
        token=write_decision.token,  # <-- token minted for a DIFFERENT (kind, intent_id)
    )
    relabel_verifies = gate.verify(relabeled_decision)
    relabel_refused = False
    try:
        executor.execute(relabeled_decision, delegate_intent_2)
    except GateViolation:
        relabel_refused = True

    # Mirror direction: also confirm a genuinely-minted delegate token
    # cannot authorize a same-intent_id write_file by just changing `kind`
    # in place (the direct in-place mutation the HISTORY.md gap describes).
    mutated_decision = Decision(
        intent_id=delegate_decision_2.intent_id,
        principal_id=delegate_decision_2.principal_id,
        kind="write_file",  # mutated from "delegate"
        permitted=delegate_decision_2.permitted,
        token=delegate_decision_2.token,  # token unchanged -- was minted for kind="delegate"
    )
    mutated_write_intent = Intent(
        kind="write_file",
        principal_id=delegate_decision_2.principal_id,
        intent_id=delegate_decision_2.intent_id,
        payload={"path": str(relabel_path), "content": "should never appear via mutation"},
    )
    mutation_verifies = gate.verify(mutated_decision)
    mutation_refused = False
    try:
        executor.execute(mutated_decision, mutated_write_intent)
    except GateViolation:
        mutation_refused = True

    results["predicate_c_kind_binding"] = {
        "relabel_case": {
            "description": "write_file token reused verbatim to authorize a delegate Decision",
            "gate_verify_result": relabel_verifies,
            "executor_refused": relabel_refused,
        },
        "mutation_case": {
            "description": "delegate Decision's kind field mutated in place to write_file, token unchanged",
            "gate_verify_result": mutation_verifies,
            "executor_refused": mutation_refused,
        },
        "relabel_target_file_absent": not relabel_path.exists(),
    }

    return results


def main() -> int:
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_mediated_delegation"]
    b = results["predicate_b_forged_delegate_refused"]
    c = results["predicate_c_kind_binding"]

    checks = [
        ("delegate decision permitted", a["delegate_decision_permitted"] is True),
        ("sub-agent file exists", a["sub_file_exists"] is True),
        ("sub-agent file content matches", a["sub_file_content_matches"] is True),
        ("sub-agent effect attributed to sub-agent principal", a["sub_effect_attributed_to_subagent"] is True),
        ("forged delegate decision refused", b["refused"] is True),
        ("forged sub-agent file absent", b["forged_sub_file_absent"] is True),
        ("relabel: Gate.verify() returns False", c["relabel_case"]["gate_verify_result"] is False),
        ("relabel: Executor refuses", c["relabel_case"]["executor_refused"] is True),
        ("mutation: Gate.verify() returns False", c["mutation_case"]["gate_verify_result"] is False),
        ("mutation: Executor refuses", c["mutation_case"]["executor_refused"] is True),
        ("relabel/mutation target file absent", c["relabel_target_file_absent"] is True),
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
