"""009 -- execution provenance combined with uid+cgroup: does the digest check hold under real
privilege separation, and does it fire BEFORE wasting real provisioning on a rejected swap.

See `lab/009-execution-provenance-with-uid-cgroup.md` for the hypothesis (with explicit null),
method, and analysis. This script runs the experiment and writes results under `out/009/`.

Combines 008 (artifact_digest bound into Decision, independently re-verified at execution time)
with 004 (uid_cgroup as a real execution class), the same way 005 combined delegation with
uid_cgroup only once both were independently proven -- not before. New question neither parent
experiment could test alone: with a real, non-trivial provisioning step (useradd, cgroup creation)
sitting between "Decision verified" and "code actually runs," does the digest check fire early
enough to avoid wasting that provisioning on an artifact that was going to be rejected anyway, or
does it only catch the swap after a real ephemeral user and cgroup already exist on the host?

Built entirely fresh -- uid/cgroup provisioning is 004's own pattern re-implemented here, not
imported (DESIGN.md SS0; HISTORY.md's no-dependencies incident). Needs real root on real Linux;
refuses cleanly everywhere else.

Run (from macOS, will refuse -- confirms the refusal path)::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/009_execution_provenance_with_uid_cgroup.py

Run for real, as root, on colima::

    colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/009_execution_provenance_with_uid_cgroup.py"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "009"

UID_RANGE_START = 64000
UID_RANGE_END = 64999
CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-exp009")


def require_real_root_linux() -> None:
    if sys.platform != "linux":
        sys.stderr.write(
            "REFUSED: this experiment requires real Linux (uid/cgroup provisioning is "
            f"Linux-specific). Detected sys.platform={sys.platform!r}. Run it on colima:\n"
            "  colima ssh -- bash -c \"cd /Users/shunhonda/dev/siphonophore && "
            "sudo python3 lab/009_execution_provenance_with_uid_cgroup.py\"\n"
        )
        sys.exit(1)
    if os.geteuid() != 0:
        sys.stderr.write(
            f"REFUSED: this experiment requires real root. Detected euid={os.geteuid()}. Re-run with sudo.\n"
        )
        sys.exit(1)
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        sys.stderr.write("REFUSED: cgroup v2 unified hierarchy not detected.\n")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Core primitives -- 008's artifact_digest + 004's uid_cgroup, combined
# ---------------------------------------------------------------------------


def digest_of(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Intent:
    kind: str  # "run_artifact"
    principal_id: str
    intent_id: str
    payload: dict
    consequence: str  # "low" | "privileged"
    artifact_code: str


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str
    artifact_digest: str
    token: str


class Gate:
    CONSEQUENCE_TO_CLASS = {"low": "same_process", "privileged": "uid_cgroup"}

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
        return intent.kind == "run_artifact" and intent.consequence in ("low", "privileged")


class GateViolation(PermissionError):
    pass


class ArtifactMismatchError(GateViolation):
    pass


class ProvisioningError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# uid+cgroup provisioning -- fresh, same pattern as 004, not imported
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _find_free_uid() -> int:
    taken = {pw.pw_uid for pw in pwd.getpwall()}
    for candidate in range(UID_RANGE_START, UID_RANGE_END + 1):
        if candidate not in taken:
            return candidate
    raise ProvisioningError(f"no free uid in reserved range [{UID_RANGE_START}, {UID_RANGE_END}]")


def provision_ephemeral_user(execution_id: str) -> tuple[str, int, int]:
    uid = _find_free_uid()
    username = f"sipho9-{execution_id[:8]}"
    result = _run([
        "useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--uid", str(uid),
        "--comment", "siphonophore ephemeral execution identity (experiment 009)", username,
    ])
    if result.returncode != 0:
        raise ProvisioningError(f"useradd failed (rc={result.returncode}): {result.stderr.strip()}")
    entry = pwd.getpwnam(username)
    if entry.pw_uid != uid:
        raise ProvisioningError(f"useradd created uid={entry.pw_uid}, expected {uid}")
    return username, entry.pw_uid, entry.pw_gid


def release_ephemeral_user(username: str) -> None:
    result = _run(["userdel", username])
    if result.returncode != 0:
        raise ProvisioningError(f"userdel failed (rc={result.returncode}): {result.stderr.strip()}")


def provision_cgroup(execution_id: str) -> Path:
    CGROUP_ROOT.mkdir(parents=True, exist_ok=True)
    cg = CGROUP_ROOT / f"exec-{execution_id}"
    cg.mkdir(parents=True, exist_ok=False)
    return cg


def add_pid_to_cgroup(cgroup_path: Path, pid: int) -> None:
    (cgroup_path / "cgroup.procs").write_text(str(pid))


def read_cgroup_procs(cgroup_path: Path) -> set[int]:
    text = (cgroup_path / "cgroup.procs").read_text()
    return {int(line) for line in text.split() if line.strip()}


def release_cgroup(cgroup_path: Path) -> None:
    remaining = read_cgroup_procs(cgroup_path)
    if remaining:
        raise ProvisioningError(f"refusing to release cgroup with live members: {remaining}")
    cgroup_path.rmdir()


def read_real_uid_from_proc(pid: int) -> int:
    status_text = Path(f"/proc/{pid}/status").read_text()
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise ProvisioningError(f"no Uid: line in /proc/{pid}/status")


_CHILD_PROGRAM_TEMPLATE = """
import json, os, sys, time
path, sync_fd = sys.argv[1], int(sys.argv[2])
os.read(sync_fd, 1)
time.sleep(0.1)
{body}
print(json.dumps({{"pid": os.getpid(), "self_reported_uid": os.getuid()}}))
"""


def _child_program(body: str) -> str:
    return _CHILD_PROGRAM_TEMPLATE.format(body=body)


_PROGRAM_A_BODY = 'open(path, "w").write("written by program A under uid_cgroup")'
_PROGRAM_B_BODY = 'open(path, "w").write("written by program B -- should never run under program A authorization")'


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

        # Provenance check FIRST, before any provisioning -- this is the actual question this
        # experiment is testing: does the digest mismatch get caught before a real useradd/cgroup
        # gets created for an artifact that was never going to be trusted, or only after.
        actual_digest = digest_of(intent.artifact_code)
        if not hmac.compare_digest(actual_digest, decision.artifact_digest):
            raise ArtifactMismatchError(
                f"artifact digest mismatch: decision authorized {decision.artifact_digest[:12]}..., "
                f"but the code about to run hashes to {actual_digest[:12]}..."
            )

        if decision.execution_class == "same_process":
            namespace = {"path": intent.payload["path"]}
            exec(intent.artifact_code, namespace)  # noqa: S102
            return {"execution_class": "same_process"}

        if decision.execution_class == "uid_cgroup":
            return self._execute_uid_cgroup(decision, intent)

        raise GateViolation(f"unknown execution_class={decision.execution_class!r}")

    def _execute_uid_cgroup(self, decision: Decision, intent: Intent) -> dict:
        execution_id = decision.intent_id
        path = intent.payload["path"]
        observations: dict = {}

        username, uid, gid = provision_ephemeral_user(execution_id)
        observations["provisioned_uid"] = uid
        cgroup_path = provision_cgroup(execution_id)

        read_fd, write_fd = os.pipe()

        def _drop_privileges() -> None:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

        proc: subprocess.Popen | None = None
        try:
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-c", intent.artifact_code, path, str(read_fd)],
                    pass_fds=(read_fd,),
                    preexec_fn=_drop_privileges,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                os.close(read_fd)
                add_pid_to_cgroup(cgroup_path, proc.pid)
                observations["real_uid_from_proc_status"] = read_real_uid_from_proc(proc.pid)
                observations["cgroup_members_while_blocked"] = sorted(read_cgroup_procs(cgroup_path))
                os.write(write_fd, b"x")
                os.close(write_fd)

                stdout, stderr = proc.communicate(timeout=10)
                observations["child_returncode"] = proc.returncode
                if proc.returncode != 0:
                    raise ProvisioningError(f"child failed: rc={proc.returncode} stderr={stderr!r}")
                observations["child_self_report"] = json.loads(stdout.strip())
                return {"execution_class": "uid_cgroup", "acting_pid": proc.pid, "observations": observations}
            finally:
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                for fd in (read_fd, write_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        finally:
            try:
                release_cgroup(cgroup_path)
                observations["cgroup_released"] = not cgroup_path.exists()
            except ProvisioningError:
                observations["cgroup_released"] = False
            release_ephemeral_user(username)
            try:
                pwd.getpwnam(username)
                observations["user_released"] = False
            except KeyError:
                observations["user_released"] = True


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-009-"))
    os.chmod(workdir, 0o777)
    results: dict = {"workdir": str(workdir), "broker_uid": os.getuid()}

    gate = Gate()
    executor = Executor(gate)
    program_a = _child_program(_PROGRAM_A_BODY)
    program_b = _child_program(_PROGRAM_B_BODY)

    # --- Predicate A: happy path -- correct artifact, uid_cgroup class, digest + identity both --
    # confirmed. --------------------------------------------------------------------------------
    a_path = workdir / "happy_path.txt"
    a_intent = Intent(
        kind="run_artifact", principal_id="principal-alice", intent_id=str(uuid.uuid4()),
        payload={"path": str(a_path)}, consequence="privileged", artifact_code=program_a,
    )
    a_decision = gate.submit(a_intent)
    a_effect = executor.execute(a_decision, a_intent)
    a_obs = a_effect["observations"]
    results["predicate_a_happy_path"] = {
        "execution_class": a_effect["execution_class"],
        "file_content_matches": a_path.exists() and a_path.read_text() == "written by program A under uid_cgroup",
        "provisioned_uid_differs_from_broker": a_obs["provisioned_uid"] != results["broker_uid"],
        "kernel_uid_matches_provisioned": a_obs["real_uid_from_proc_status"] == a_obs["provisioned_uid"],
        "self_report_uid_matches_kernel": a_obs["child_self_report"]["self_reported_uid"] == a_obs["real_uid_from_proc_status"],
        "digest_bound_in_decision": a_decision.artifact_digest == digest_of(program_a),
        "cgroup_released": a_obs["cgroup_released"],
        "user_released": a_obs["user_released"],
    }

    # --- Predicate B: swapped artifact under uid_cgroup -- authorize A, present B. Real question:
    # does the mismatch get caught BEFORE a real user/cgroup is provisioned for B. ----------------
    b_path = workdir / "swapped.txt"
    b_real_intent = Intent(
        kind="run_artifact", principal_id="principal-alice", intent_id=str(uuid.uuid4()),
        payload={"path": str(b_path)}, consequence="privileged", artifact_code=program_a,
    )
    b_decision = gate.submit(b_real_intent)
    b_swapped_intent = Intent(
        intent_id=b_real_intent.intent_id, principal_id=b_real_intent.principal_id, kind=b_real_intent.kind,
        payload=b_real_intent.payload, consequence=b_real_intent.consequence, artifact_code=program_b,
    )
    users_before_b = {pw.pw_name for pw in pwd.getpwall()}
    cgroups_before_b = set(CGROUP_ROOT.iterdir()) if CGROUP_ROOT.exists() else set()
    b_raised = None
    try:
        executor.execute(b_decision, b_swapped_intent)
    except ArtifactMismatchError as exc:
        b_raised = str(exc)
    users_after_b = {pw.pw_name for pw in pwd.getpwall()}
    cgroups_after_b = set(CGROUP_ROOT.iterdir()) if CGROUP_ROOT.exists() else set()
    results["predicate_b_swapped_artifact_refused_before_provisioning"] = {
        "raised_artifact_mismatch": b_raised is not None,
        "error": b_raised,
        "file_absent": not b_path.exists(),
        "no_new_users_provisioned": users_after_b == users_before_b,
        "no_new_cgroups_provisioned": cgroups_after_b == cgroups_before_b,
    }

    # --- Predicate C: forged Decision (never through Gate.submit()) refused before provisioning. -
    c_path = workdir / "forged.txt"
    c_intent = Intent(
        kind="run_artifact", principal_id="principal-mallory", intent_id=str(uuid.uuid4()),
        payload={"path": str(c_path)}, consequence="privileged", artifact_code=program_a,
    )
    c_forged = Decision(
        intent_id=c_intent.intent_id, principal_id=c_intent.principal_id, kind=c_intent.kind,
        permitted=True, execution_class="uid_cgroup", artifact_digest=digest_of(program_a),
        token="beadfeed" * 8,
    )
    users_before_c = {pw.pw_name for pw in pwd.getpwall()}
    c_refused = False
    try:
        executor.execute(c_forged, c_intent)
    except GateViolation:
        c_refused = True
    users_after_c = {pw.pw_name for pw in pwd.getpwall()}
    results["predicate_c_forged_refused"] = {
        "refused": c_refused, "file_absent": not c_path.exists(),
        "no_new_users_provisioned": users_after_c == users_before_c,
    }

    # --- Predicate D: digest-tamper replay, combined with uid_cgroup. ---------------------------
    d_path = workdir / "digest_tamper.txt"
    d_intent = Intent(
        kind="run_artifact", principal_id="principal-bob", intent_id=str(uuid.uuid4()),
        payload={"path": str(d_path)}, consequence="privileged", artifact_code=program_a,
    )
    d_genuine = gate.submit(d_intent)
    d_tampered = Decision(
        intent_id=d_genuine.intent_id, principal_id=d_genuine.principal_id, kind=d_genuine.kind,
        permitted=d_genuine.permitted, execution_class=d_genuine.execution_class,
        artifact_digest=digest_of(program_b), token=d_genuine.token,
    )
    d_verifies = gate.verify(d_tampered)
    d_refused = False
    try:
        executor.execute(d_tampered, d_intent)
    except GateViolation:
        d_refused = True
    results["predicate_d_digest_tamper_refused"] = {
        "gate_verify_result": d_verifies, "executor_refused": d_refused, "file_absent": not d_path.exists(),
    }

    return results


def main() -> int:
    require_real_root_linux()
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_happy_path"]
    b = results["predicate_b_swapped_artifact_refused_before_provisioning"]
    c = results["predicate_c_forged_refused"]
    d = results["predicate_d_digest_tamper_refused"]

    checks = [
        ("happy path: file content matches program A", a["file_content_matches"] is True),
        ("happy path: provisioned uid differs from broker", a["provisioned_uid_differs_from_broker"] is True),
        ("happy path: kernel uid matches provisioned", a["kernel_uid_matches_provisioned"] is True),
        ("happy path: self-report matches kernel", a["self_report_uid_matches_kernel"] is True),
        ("happy path: digest bound in Decision", a["digest_bound_in_decision"] is True),
        ("happy path: cgroup released", a["cgroup_released"] is True),
        ("happy path: user released", a["user_released"] is True),
        ("swapped: ArtifactMismatchError raised", b["raised_artifact_mismatch"] is True),
        ("swapped: target file absent", b["file_absent"] is True),
        ("swapped: NO new user provisioned for rejected artifact", b["no_new_users_provisioned"] is True),
        ("swapped: NO new cgroup provisioned for rejected artifact", b["no_new_cgroups_provisioned"] is True),
        ("forged: refused", c["refused"] is True),
        ("forged: no new users provisioned", c["no_new_users_provisioned"] is True),
        ("digest tamper: Gate.verify() returns False", d["gate_verify_result"] is False),
        ("digest tamper: Executor refuses", d["executor_refused"] is True),
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
