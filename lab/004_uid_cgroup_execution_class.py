"""Reproducer for lab/004 -- uid+cgroup as a real third execution class.

Extends lab/003's same_process/separate_process pair with "uid_cgroup", reusing the archived v1
identity.py primitives (real useradd/cgroupfs provisioning, already validated on colima in the
prior project phase) as the actual Executor backend -- not reimplemented, imported directly from
archive/v1-mediation-orchestrator/siphonophore/identity.py.

Needs real root on real Linux. Unlike lab/001-003, this cannot stay portable and does NOT
pretend to: if not running as root on Linux, the script prints why and exits nonzero rather than
silently reporting a result it didn't actually check -- the Trusted Enough to Run discipline
DESIGN.md section 4 names, applied to this experiment's own execution, not just its subject
matter. Actually run on colima, not just written to run there.

execution_class="uid_cgroup" is a new VALUE on an already-bound field (execution_class has been
bound into the Decision token since lab/003), not a new field -- so unlike 002 (new field: kind)
and 003 (new field: execution_class), there is no new binding surface to add here. Tested anyway
whether that's actually true, not assumed: a genuinely-minted uid_cgroup Decision is downgrade-
replayed to same_process, same shape as 003's downgrade test, now for the strongest tier.

Run (needs real root on real Linux, e.g. colima):

    cd ~/dev/siphonophore
    sudo python3 lab/004_uid_cgroup_execution_class.py
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
OUT_DIR = REPO_ROOT / "lab" / "out" / "004"
SANDBOX = OUT_DIR / "sandbox"

# Reuse the archived v1 identity.py directly -- real useradd/cgroupfs, already validated on colima
# in the prior project phase. Not reimplemented here. Loaded as a standalone module bypassing the
# archived siphonophore/__init__.py on purpose: that package's __init__ still imports
# orchestrator.py, which imports strands -- exactly the dependency DESIGN.md's revision dropped.
# identity.py itself has no such import; only the package wrapper around it does.
import importlib.util

_ARCHIVE_IDENTITY_PATH = REPO_ROOT / "archive" / "v1-mediation-orchestrator" / "siphonophore" / "identity.py"
_spec = importlib.util.spec_from_file_location("_archived_identity", _ARCHIVE_IDENTITY_PATH)
identity = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = identity  # dataclass() needs this registered before exec_module runs
_spec.loader.exec_module(identity)


def _require_real_root_linux() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        print(
            "lab/004 needs real root on real Linux (useradd, cgroupfs) -- refusing to report a "
            "result it did not actually check. Run this on colima: "
            "`colima ssh -- bash -c 'cd ~/dev/siphonophore && sudo python3 lab/004_uid_cgroup_execution_class.py'`",
            file=sys.stderr,
        )
        sys.exit(2)


# ---------------------------------------------------------------------------
# Intent / Decision / Gate / Executor -- evolved from lab/003
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    principal_id: str
    kind: str
    consequence: str  # "low" | "high" | "critical"
    payload: dict


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    execution_class: str  # "same_process" | "separate_process" | "uid_cgroup"
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
        if intent.consequence == "critical":
            return "uid_cgroup"
        if intent.consequence == "high":
            return "separate_process"
        return "same_process"

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
        if decision.execution_class == "uid_cgroup":
            return self._write_uid_cgroup(decision.intent_id, payload)
        raise ValueError(f"unknown execution_class {decision.execution_class!r}")

    def _write_same_process(self, payload: dict) -> dict:
        path = Path(payload["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["content"])
        return {"path": str(path), "pid": os.getpid(), "uid": os.getuid()}

    def _write_separate_process(self, payload: dict) -> dict:
        script = (
            "import json, os, pathlib\n"
            f"p = pathlib.Path({payload['path']!r})\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            f"p.write_text({payload['content']!r})\n"
            "print(json.dumps({'path': str(p), 'pid': os.getpid(), 'uid': os.getuid()}))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def _write_uid_cgroup(self, intent_id: str, payload: dict) -> dict:
        """Provisioning happens here, at dispatch time -- after verify()/permitted have already
        passed, mirroring identity.py's own 'provision at delegation time, not organically'
        principle and v1's Colony._dispatch_severed shape. No new Decision field needed: the
        Decision only had to say uid_cgroup was granted; WHICH uid gets assigned is decided now,
        by code already gated behind the same verification everything else in this Executor is."""
        node_id = f"lab004-{intent_id[:8]}"
        ident = identity.provision_identity(node_id)
        try:
            script = (
                "import json, os, pathlib\n"
                f"p = pathlib.Path({payload['path']!r})\n"
                "p.parent.mkdir(parents=True, exist_ok=True)\n"
                f"p.write_text({payload['content']!r})\n"
                "print(json.dumps({'path': str(p), 'pid': os.getpid(), 'uid': os.getuid()}))\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                user=ident.uid,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            identity.add_pid_to_cgroup(ident.cgroup_path, proc.pid)
            members_while_running = identity.cgroup_pids(ident.cgroup_path)
            stdout, stderr = proc.communicate(timeout=10)
            if proc.returncode != 0:
                raise RuntimeError(f"uid_cgroup subprocess exited {proc.returncode}: {stderr}")
            result = json.loads(stdout)
            result["provisioned_uid"] = ident.uid
            result["cgroup_path"] = ident.cgroup_path
            result["pid_was_in_cgroup_while_running"] = proc.pid in members_while_running
            return result
        finally:
            identity.release_identity(ident)


# ---------------------------------------------------------------------------
# The experiment itself
# ---------------------------------------------------------------------------


def main() -> int:
    _require_real_root_linux()

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)

    gate = Gate()
    executor = Executor(gate)
    results: dict = {}
    broker_uid = os.getuid()

    # --- Attempt 1: critical consequence -> uid_cgroup ---------------------------------------
    critical_intent = Intent(
        principal_id="principal:test",
        kind="file_write",
        consequence="critical",
        payload={"path": str(SANDBOX / "critical.txt"), "content": "critical consequence, real uid+cgroup"},
    )
    critical_decision = gate.submit(critical_intent)
    outcome = executor.execute(critical_decision, critical_intent.payload)
    results["critical_consequence"] = {
        "selected_execution_class": critical_decision.execution_class,
        "reported_uid": outcome["uid"],
        "broker_uid": broker_uid,
        "ran_under_a_different_provisioned_uid": outcome["uid"] != broker_uid
        and outcome["uid"] == outcome["provisioned_uid"]
        and identity.NODE_UID_MIN <= outcome["uid"] <= identity.NODE_UID_MAX,
        "pid_was_in_cgroup_while_running": outcome["pid_was_in_cgroup_while_running"],
        "ground_truth_file_content_correct": (
            Path(outcome["path"]).read_text() == "critical consequence, real uid+cgroup"
        ),
        "identity_released_cgroup_removed": not Path(outcome["cgroup_path"]).exists(),
    }

    # --- Attempt 2: forged Decision claiming uid_cgroup ---------------------------------------
    forged = Decision(
        intent_id=str(uuid.uuid4()),
        principal_id="principal:test",
        kind="file_write",
        execution_class="uid_cgroup",
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

    # --- Attempt 3: downgrade a genuine uid_cgroup grant to same_process ----------------------
    real_critical_intent = Intent(
        principal_id="principal:test",
        kind="file_write",
        consequence="critical",
        payload={"path": str(SANDBOX / "legit-critical.txt"), "content": "genuinely critical"},
    )
    real_critical_decision = gate.submit(real_critical_intent)
    downgraded = Decision(
        intent_id=real_critical_decision.intent_id,
        principal_id=real_critical_decision.principal_id,
        kind=real_critical_decision.kind,
        execution_class="same_process",  # relabeled DOWN from the strongest tier
        permitted=real_critical_decision.permitted,
        token=real_critical_decision.token,
    )
    replay_payload = {"path": str(SANDBOX / "downgraded-replay.txt"), "content": "should not exist"}
    try:
        outcome2 = executor.execute(downgraded, replay_payload)
        results["uid_cgroup_downgrade_replay"] = {
            "executor_raised": None,
            "ran_as_broker_uid": outcome2.get("uid") == broker_uid,
            "ground_truth_file_exists": Path(replay_payload["path"]).exists(),
        }
    except Exception as exc:  # noqa: BLE001
        results["uid_cgroup_downgrade_replay"] = {
            "executor_raised": f"{type(exc).__name__}: {exc}",
            "ground_truth_file_exists": Path(replay_payload["path"]).exists(),
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUT_DIR / "results.json"
    results_path.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\nResults written to {results_path}")

    critical_ok = (
        results["critical_consequence"]["ran_under_a_different_provisioned_uid"]
        and results["critical_consequence"]["pid_was_in_cgroup_while_running"]
        and results["critical_consequence"]["ground_truth_file_content_correct"]
        and results["critical_consequence"]["identity_released_cgroup_removed"]
    )
    forged_blocked = (
        results["forged_bypass"]["executor_raised"] is not None and not results["forged_bypass"]["ground_truth_file_exists"]
    )
    downgrade_blocked = (
        results["uid_cgroup_downgrade_replay"]["executor_raised"] is not None
        and not results["uid_cgroup_downgrade_replay"]["ground_truth_file_exists"]
    )

    print(f"\nCritical-consequence intent ran under a real, distinct provisioned uid, in-cgroup: {critical_ok}")
    print(f"Forged uid_cgroup Decision was blocked: {forged_blocked}")
    print(f"uid_cgroup downgrade replay was blocked: {downgrade_blocked}")
    all_pass = critical_ok and forged_blocked and downgrade_blocked
    print(f"\nHYPOTHESIS {'SUPPORTED' if all_pass else 'FALSIFIED'}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
