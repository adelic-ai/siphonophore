"""001 -- Gate blocks unmediated effects.

See `lab/001-gate-blocks-unmediated-effects.md` for the hypothesis (with
explicit null), method, and analysis. This script runs the experiment and
writes results under `out/001/`.

Minimal `Intent -> Gate.submit() -> Decision -> Executor.execute()` shape
(DESIGN.md SS1, SS7). `Decision` carries an HMAC token only `Gate.submit()`
can validly mint, keyed by a secret that never leaves the Gate instance.
`Executor.execute()` independently verifies that token before producing any
effect -- it does not trust a Decision merely because one was handed to it.

Stdlib only. No imports outside this file (DESIGN.md SS0; HISTORY.md's
no-dependencies incident).

Run::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/001_gate_blocks_unmediated_effects.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "001"


# ---------------------------------------------------------------------------
# Core primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    """What a principal wants to become a real-world effect."""

    kind: str
    principal_id: str
    intent_id: str
    payload: dict


@dataclass(frozen=True)
class Decision:
    """A Gate's ruling on an Intent, carrying a token binding the fields the
    Executor will branch on. The token can only be produced by a Gate that
    holds the signing secret; anyone can construct a Decision object with
    arbitrary field values, but only Gate-minted tokens verify."""

    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    token: str


class Gate:
    """The single mediation authority. Holds a secret that never leaves this
    instance -- no accessor exposes it, and Executor only ever calls
    `gate.verify(decision)`, never reads `gate._secret` directly."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(self, intent_id: str, principal_id: str, kind: str, permitted: bool) -> bytes:
        # Every field Executor branches on must be in this message (DESIGN.md SS2).
        return f"{intent_id}:{principal_id}:{kind}:{permitted}".encode("utf-8")

    def _mint(self, intent_id: str, principal_id: str, kind: str, permitted: bool) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        """The only way to obtain a validly-tokened Decision."""
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
        """Independent re-derivation of the token from the Decision's own
        fields. True only if `decision` was actually minted by this Gate
        instance with these exact field values."""
        expected = self._mint(decision.intent_id, decision.principal_id, decision.kind, decision.permitted)
        return hmac.compare_digest(expected, decision.token)

    def _policy(self, intent: Intent) -> bool:
        # Default-allow policy for this minimal experiment -- SS7 asks only
        # that the Gate be the sole path to a valid token, not that policy
        # be sophisticated yet.
        return intent.kind in ("write_file",)


class GateViolation(PermissionError):
    """Raised by Executor when a Decision fails its own verification."""


class Executor:
    """Produces effects, but only for Decisions that verify against the Gate
    that supposedly issued them. Never trusts `decision.permitted` on its
    own -- an attacker can set that field to True on a hand-built object."""

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

        raise GateViolation(f"no executor handler for kind={intent.kind!r}")


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-001-"))
    results: dict = {"workdir": str(workdir)}

    gate = Gate()
    executor = Executor(gate)

    # --- Predicate A: mediated write succeeds, confirmed by reading the
    # actual file back (ground truth, not "no exception raised"). ---------
    mediated_path = workdir / "mediated.txt"
    content = f"written via Gate, nonce={uuid.uuid4().hex}"
    intent = Intent(
        kind="write_file",
        principal_id="principal-alice",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(mediated_path), "content": content},
    )
    decision = gate.submit(intent)
    effect = executor.execute(decision, intent)

    mediated_file_exists = mediated_path.exists()
    mediated_file_content_matches = mediated_file_exists and mediated_path.read_text() == content
    results["predicate_a_mediated_write"] = {
        "decision_permitted": decision.permitted,
        "effect": effect,
        "file_exists": mediated_file_exists,
        "file_content_matches": mediated_file_content_matches,
    }

    # --- Predicate B: a hand-forged Decision (never through Gate.submit())
    # is refused by Executor.execute()'s own verification, and no file is
    # written. Forged two ways: (1) a plausible-looking but made-up token,
    # (2) a token computed with an attacker's own secret (simulating an
    # attacker who built their own HMAC scheme but doesn't have the Gate's
    # actual key). ----------------------------------------------------------
    forged_path = workdir / "forged.txt"
    forged_intent = Intent(
        kind="write_file",
        principal_id="principal-mallory",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(forged_path), "content": "should never appear on disk"},
    )

    forged_variants = []

    # Variant 1: made-up hex string as the token.
    forged_decision_1 = Decision(
        intent_id=forged_intent.intent_id,
        principal_id=forged_intent.principal_id,
        kind=forged_intent.kind,
        permitted=True,
        token="0" * 64,
    )
    refused_1 = False
    try:
        executor.execute(forged_decision_1, forged_intent)
    except GateViolation:
        refused_1 = True
    forged_variants.append({"variant": "made_up_token", "refused": refused_1})

    # Variant 2: attacker mints their own HMAC with a *different* secret --
    # i.e., attacker never had access to the real Gate's secret (it never
    # left the Gate instance) and can only guess/attempt their own key.
    attacker_secret = secrets.token_bytes(32)
    attacker_msg = f"{forged_intent.intent_id}:{forged_intent.principal_id}:{forged_intent.kind}:True".encode()
    attacker_token = hmac.new(attacker_secret, attacker_msg, hashlib.sha256).hexdigest()
    forged_decision_2 = Decision(
        intent_id=forged_intent.intent_id,
        principal_id=forged_intent.principal_id,
        kind=forged_intent.kind,
        permitted=True,
        token=attacker_token,
    )
    refused_2 = False
    try:
        executor.execute(forged_decision_2, forged_intent)
    except GateViolation:
        refused_2 = True
    forged_variants.append({"variant": "attacker_own_secret", "refused": refused_2})

    forged_file_absent = not forged_path.exists()
    results["predicate_b_forged_refused"] = {
        "variants": forged_variants,
        "forged_file_absent": forged_file_absent,
    }

    # --- Extra ground-truth check: confirm the Gate's secret really is
    # inaccessible via any public attribute -- i.e. the class doesn't leak
    # it by convention (best-effort static check of the instance's public
    # surface). This isn't a security proof, just documents what "never
    # leaves the Gate instance" means operationally in this experiment. ---
    public_attrs = [a for a in dir(gate) if not a.startswith("_")]
    results["gate_public_surface"] = public_attrs

    return results


def main() -> int:
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_mediated_write"]
    b = results["predicate_b_forged_refused"]

    ok = True
    checks = [
        ("mediated write permitted", a["decision_permitted"] is True),
        ("mediated file exists", a["file_exists"] is True),
        ("mediated file content matches", a["file_content_matches"] is True),
        ("forged variant 1 refused", b["variants"][0]["refused"] is True),
        ("forged variant 2 refused", b["variants"][1]["refused"] is True),
        ("forged file absent", b["forged_file_absent"] is True),
    ]
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # Clean up the scratch workdir (results.json under out/ already captures
    # what we need; no reason to leave temp files scattered).
    shutil.rmtree(results["workdir"], ignore_errors=True)

    if not ok:
        print("HYPOTHESIS NOT SUPPORTED", file=sys.stderr)
        return 1

    print("HYPOTHESIS SUPPORTED")
    return 0


if __name__ == "__main__":
    assert Gate is not None  # sanity: primitives defined above, not imported
    raise SystemExit(main())
