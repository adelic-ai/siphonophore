"""004 -- uid+cgroup as a real third execution class.

See `lab/004-uid-cgroup-execution-class.md` for the hypothesis (with
explicit null), method, and analysis. This script runs the experiment and
writes results under `out/004/`.

Adds `uid_cgroup` as a third `execution_class`, alongside 003's
`same_process` and `separate_process`. Unlike those, this class needs real
root on real Linux -- it provisions an ephemeral, no-login system user in a
reserved uid range, creates a real cgroup v2 leaf directory, and moves the
spawned effect's real pid into it -- and this script does NOT pretend to be
portable. `require_real_root_linux()` below refuses to run anywhere else,
with a clear explanation, and that refusal path is exercised (and its
result recorded) before any provisioning code runs.

Built entirely fresh for this experiment. Per HISTORY.md's account of the
one time the no-dependencies principle was violated -- reusing v1's
`identity.py` from the deleted `archive/v1-mediation-orchestrator/` tree,
which led to deleting that whole archive rather than patching around the
reuse -- this script does not import, copy, adapt, or "remember" that
file's code in any way. The uid/cgroup provisioning logic below is designed
and written fresh, using only the Python standard library plus the real
`useradd`/`userdel` system binaries (called as external commands, the way
any operator would from a shell -- not vendored code).

Same discipline as 002/003: every field Executor.execute() branches on
(kind, execution_class) is bound into the HMAC from the first line of this
file's Gate implementation. Forged-Decision and downgrade-replay tests are
extended to this class.

Run (from macOS, will refuse -- confirms the refusal path)::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/004_uid_cgroup_execution_class.py

Run for real, as root, on colima (a real root-capable Linux VM)::

    colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/004_uid_cgroup_execution_class.py"
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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "004"

# Reserved uid range for siphonophore's own ephemeral provisioned users.
# Confirmed free on the target colima Ubuntu image at experiment time (see
# write-up Method section) -- not systemd's DynamicUser range (61184-65519)
# or any standard SYS_UID range (100-999), to avoid collision with anything
# the base image or its package manager might allocate later.
UID_RANGE_START = 62000
UID_RANGE_END = 62999

CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-exp004")


# ---------------------------------------------------------------------------
# Startup gate: refuse anywhere that isn't real root on real Linux
# ---------------------------------------------------------------------------


def require_real_root_linux() -> None:
    """Exit nonzero with a clear explanation unless this process is really
    root on a real Linux kernel with cgroup v2 mounted. Must be checked
    (and its refusal path confirmed working) before any provisioning."""
    if sys.platform != "linux":
        sys.stderr.write(
            "REFUSED: this experiment requires real Linux (uid/cgroup provisioning is "
            f"Linux-specific: useradd, /proc/<pid>/status, cgroup v2). Detected "
            f"sys.platform={sys.platform!r}. Run it on a real Linux host, e.g. via colima:\n"
            "  colima ssh -- bash -c \"cd /Users/shunhonda/dev/siphonophore && "
            "sudo python3 lab/004_uid_cgroup_execution_class.py\"\n"
        )
        sys.exit(1)

    if os.geteuid() != 0:
        sys.stderr.write(
            "REFUSED: this experiment requires real root (creating a system user via "
            "useradd, and writing another process's pid into a cgroup's cgroup.procs, "
            f"both require privilege). Detected euid={os.geteuid()}. Re-run with sudo.\n"
        )
        sys.exit(1)

    controllers_file = Path("/sys/fs/cgroup/cgroup.controllers")
    if not controllers_file.exists():
        sys.stderr.write(
            "REFUSED: cgroup v2 unified hierarchy not detected (no "
            f"{controllers_file} file). This experiment requires cgroup v2.\n"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Core primitives (same shape as 001-003, extended with uid_cgroup)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    kind: str  # "write_file"
    principal_id: str
    intent_id: str
    payload: dict
    consequence: str  # "low" | "high" | "privileged"


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str  # "same_process" | "separate_process" | "uid_cgroup"
    token: str


class Gate:
    CONSEQUENCE_TO_CLASS = {
        "low": "same_process",
        "high": "separate_process",
        "privileged": "uid_cgroup",
    }

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str
    ) -> bytes:
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
        return intent.kind in ("write_file",) and intent.consequence in ("low", "high", "privileged")


class GateViolation(PermissionError):
    pass


# ---------------------------------------------------------------------------
# uid+cgroup provisioning -- fresh, self-contained, this file only
# ---------------------------------------------------------------------------


class ProvisioningError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _find_free_uid() -> int:
    taken = {pw.pw_uid for pw in pwd.getpwall()}
    for candidate in range(UID_RANGE_START, UID_RANGE_END + 1):
        if candidate not in taken:
            return candidate
    raise ProvisioningError(f"no free uid in reserved range [{UID_RANGE_START}, {UID_RANGE_END}]")


def provision_ephemeral_user(execution_id: str) -> tuple[str, int, int]:
    """Create a real, ephemeral, no-login system user via the real
    `useradd` binary, in the reserved uid range. Returns (username, uid,
    gid). Caller is responsible for calling release_ephemeral_user()."""
    uid = _find_free_uid()
    username = f"sipho-{execution_id[:8]}"

    result = _run(
        [
            "useradd",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            "--uid",
            str(uid),
            "--comment",
            "siphonophore ephemeral execution identity (experiment 004)",
            username,
        ]
    )
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
    """Create a real cgroup v2 leaf directory for this execution."""
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
    # A cgroup v2 directory can only be rmdir'd when cgroup.procs is empty
    # (the kernel enforces this). By the time we call this, the process we
    # added should already have exited and been auto-removed.
    remaining = read_cgroup_procs(cgroup_path)
    if remaining:
        raise ProvisioningError(f"refusing to release cgroup with live members: {remaining}")
    cgroup_path.rmdir()


def read_real_uid_from_proc(pid: int) -> int:
    """Ground truth: the KERNEL's own record of a process's real uid, read
    by root from /proc/<pid>/status -- not the process's own self-reported
    os.getuid(). This is what an external observer (DESIGN.md SS5) would
    read too, with zero siphonophore-specific code."""
    status_text = Path(f"/proc/{pid}/status").read_text()
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            # Format: "Uid:\treal\teffective\tsaved\tfs"
            parts = line.split()
            return int(parts[1])
    raise ProvisioningError(f"no Uid: line in /proc/{pid}/status")


# Child program: blocks on an inherited pipe fd until the parent signals
# (giving the parent a window to add this pid to the cgroup and verify
# membership + real uid while the process is provably still alive), then
# performs the effect and self-reports what it observes about itself.
_CHILD_PROGRAM = """
import json, os, sys, time

path, content, sync_fd = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.read(sync_fd, 1)  # block until parent releases us
time.sleep(0.2)       # widen the parent's verification window a bit further

with open(path, "w") as f:
    f.write(content)

cgroup_self = open("/proc/self/cgroup").read().strip()
print(json.dumps({
    "pid": os.getpid(),
    "self_reported_uid": os.getuid(),
    "self_reported_gid": os.getgid(),
    "path": path,
    "cgroup_self": cgroup_self,
}))
"""


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

        if intent.kind != "write_file":
            raise GateViolation(f"no executor handler for kind={intent.kind!r}")

        path = intent.payload["path"]
        content = intent.payload["content"]

        if decision.execution_class == "same_process":
            with open(path, "w") as f:
                f.write(content)
            return {"effect": "write_file", "execution_class": "same_process", "acting_pid": os.getpid()}

        if decision.execution_class == "separate_process":
            proc = subprocess.run(
                [sys.executable, "-c", "import json,os,sys; open(sys.argv[1],'w').write(sys.argv[2]); "
                 "print(json.dumps({'pid': os.getpid()}))", path, content],
                capture_output=True, text=True, check=True,
            )
            report = json.loads(proc.stdout.strip())
            return {"effect": "write_file", "execution_class": "separate_process", "acting_pid": report["pid"]}

        if decision.execution_class == "uid_cgroup":
            return self._execute_uid_cgroup(decision, path, content)

        raise GateViolation(f"unknown execution_class={decision.execution_class!r}")

    def _execute_uid_cgroup(self, decision: Decision, path: str, content: str) -> dict:
        execution_id = decision.intent_id  # stable correlation id for this execution
        observations: dict = {}

        username, uid, gid = provision_ephemeral_user(execution_id)
        observations["provisioned_username"] = username
        observations["provisioned_uid"] = uid
        observations["provisioned_gid"] = gid

        cgroup_path = provision_cgroup(execution_id)
        observations["cgroup_path"] = str(cgroup_path)

        read_fd, write_fd = os.pipe()

        def _drop_privileges() -> None:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", _CHILD_PROGRAM, path, content, str(read_fd)],
                pass_fds=(read_fd,),
                preexec_fn=_drop_privileges,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(read_fd)  # parent's copy no longer needed; child inherited its own

            # --- Ground truth, while the process is confirmed alive ---
            add_pid_to_cgroup(cgroup_path, proc.pid)
            members_while_blocked = read_cgroup_procs(cgroup_path)
            real_uid_from_kernel = read_real_uid_from_proc(proc.pid)
            still_alive_at_check_time = proc.poll() is None

            observations["cgroup_members_while_blocked"] = sorted(members_while_blocked)
            observations["child_pid_in_cgroup_while_blocked"] = proc.pid in members_while_blocked
            observations["real_uid_from_proc_status"] = real_uid_from_kernel
            observations["still_alive_at_membership_check"] = still_alive_at_check_time

            # Release the child now that ground truth has been captured.
            os.write(write_fd, b"x")
            os.close(write_fd)

            stdout, stderr = proc.communicate(timeout=10)
            observations["child_returncode"] = proc.returncode
            if proc.returncode != 0:
                raise ProvisioningError(f"child process failed: rc={proc.returncode} stderr={stderr!r}")
            child_self_report = json.loads(stdout.strip())
            observations["child_self_report"] = child_self_report
        finally:
            # Best-effort fd cleanup in case of an exception before close().
            for fd in (read_fd, write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass

        # --- Clean release ---
        release_cgroup(cgroup_path)
        observations["cgroup_released"] = not cgroup_path.exists()
        release_ephemeral_user(username)
        try:
            pwd.getpwnam(username)
            observations["user_released"] = False
        except KeyError:
            observations["user_released"] = True

        return {
            "effect": "write_file",
            "execution_class": "uid_cgroup",
            "acting_pid": proc.pid,
            "observations": observations,
        }


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-004-"))
    # tempfile.mkdtemp() as root creates a 0700 directory. The uid_cgroup
    # effect below genuinely drops privileges to a real, unprivileged
    # provisioned uid before writing -- so unlike 001-003 (where the
    # process performing the write was always root or a child of root's
    # own process tree without a uid change), that write only succeeds if
    # the target directory actually grants the provisioned uid write
    # access. Widened deliberately, not as a security shortcut: this is a
    # disposable per-run scratch directory under a real tempdir, and the
    # experiment's own point is testing what the *provisioned identity* can
    # do, not what root can do. See write-up Analysis for the real
    # PermissionError this surfaced on the first run.
    os.chmod(workdir, 0o777)
    results: dict = {
        "workdir": str(workdir),
        "broker_pid": os.getpid(),
        "broker_uid": os.getuid(),
    }

    gate = Gate()
    executor = Executor(gate)

    # --- Predicate A: mediated uid_cgroup write succeeds; file content
    # confirmed on disk (ground truth). ------------------------------------
    priv_path = workdir / "privileged_effect.txt"
    priv_content = f"written under a provisioned uid, nonce={uuid.uuid4().hex}"
    priv_intent = Intent(
        kind="write_file",
        principal_id="principal-alice",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(priv_path), "content": priv_content},
        consequence="privileged",
    )
    priv_decision = gate.submit(priv_intent)
    assert priv_decision.execution_class == "uid_cgroup"
    priv_effect = executor.execute(priv_decision, priv_intent)
    obs = priv_effect["observations"]

    results["predicate_a_mediated_uid_cgroup_write"] = {
        "execution_class_assigned": priv_decision.execution_class,
        "file_exists": priv_path.exists(),
        "file_content_matches": priv_path.exists() and priv_path.read_text() == priv_content,
        "effect": priv_effect,
    }

    # --- Predicate B: distinct provisioned uid, confirmed by the ROOT
    # PARENT reading /proc/<pid>/status (kernel ground truth), not the
    # child's own self-report -- and the two independent sources (kernel
    # vs. child self-report) agree. -----------------------------------------
    results["predicate_b_distinct_uid_ground_truth"] = {
        "provisioned_uid": obs["provisioned_uid"],
        "broker_uid": results["broker_uid"],
        "real_uid_from_proc_status": obs["real_uid_from_proc_status"],
        "child_self_reported_uid": obs["child_self_report"]["self_reported_uid"],
        "provisioned_uid_differs_from_broker": obs["provisioned_uid"] != results["broker_uid"],
        "kernel_ground_truth_matches_provisioned_uid": obs["real_uid_from_proc_status"] == obs["provisioned_uid"],
        "kernel_ground_truth_matches_child_self_report": (
            obs["real_uid_from_proc_status"] == obs["child_self_report"]["self_reported_uid"]
        ),
    }

    # --- Predicate C: real cgroup membership confirmed WHILE the process
    # was still alive (not after the fact). ---------------------------------
    results["predicate_c_cgroup_membership_while_running"] = {
        "still_alive_at_check": obs["still_alive_at_membership_check"],
        "child_pid_in_cgroup_while_blocked": obs["child_pid_in_cgroup_while_blocked"],
        "cgroup_members_observed": obs["cgroup_members_while_blocked"],
    }

    # --- Predicate D: clean release afterward -- cgroup directory gone,
    # ephemeral user's passwd entry gone. -----------------------------------
    results["predicate_d_clean_release"] = {
        "cgroup_released": obs["cgroup_released"],
        "user_released": obs["user_released"],
    }

    # --- Predicate E: forged Decision claiming uid_cgroup is refused, and
    # NO provisioning side effects occur at all (no user created, no
    # cgroup directory created, no file written). ---------------------------
    forged_path = workdir / "forged.txt"
    forged_intent = Intent(
        kind="write_file",
        principal_id="principal-mallory",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(forged_path), "content": "should never appear"},
        consequence="privileged",
    )
    forged_decision = Decision(
        intent_id=forged_intent.intent_id,
        principal_id=forged_intent.principal_id,
        kind=forged_intent.kind,
        permitted=True,
        execution_class="uid_cgroup",
        token="beadfeed" * 8,
    )
    users_before = {pw.pw_name for pw in pwd.getpwall()}
    cgroups_before = set(CGROUP_ROOT.iterdir()) if CGROUP_ROOT.exists() else set()
    forged_refused = False
    try:
        executor.execute(forged_decision, forged_intent)
    except GateViolation:
        forged_refused = True
    users_after = {pw.pw_name for pw in pwd.getpwall()}
    cgroups_after = set(CGROUP_ROOT.iterdir()) if CGROUP_ROOT.exists() else set()

    results["predicate_e_forged_refused_no_side_effects"] = {
        "refused": forged_refused,
        "forged_file_absent": not forged_path.exists(),
        "no_new_users_provisioned": users_after == users_before,
        "no_new_cgroups_provisioned": cgroups_after == cgroups_before,
    }

    # --- Predicate F: downgrade-replay. A genuinely-minted uid_cgroup
    # Decision, relabeled to same_process with the token left unchanged,
    # fails Gate.verify() and is refused -- no privileged provisioning is
    # attempted (same_process doesn't provision anything, but crucially the
    # ORIGINAL uid_cgroup authorization must not be honored under a
    # different, unverified label either). ----------------------------------
    downgrade_path = workdir / "downgrade.txt"
    downgrade_intent = Intent(
        kind="write_file",
        principal_id="principal-bob",
        intent_id=str(uuid.uuid4()),
        payload={"path": str(downgrade_path), "content": "should never appear via downgrade"},
        consequence="privileged",
    )
    genuine_uid_cgroup_decision = gate.submit(downgrade_intent)
    assert genuine_uid_cgroup_decision.execution_class == "uid_cgroup"

    downgraded_decision = Decision(
        intent_id=genuine_uid_cgroup_decision.intent_id,
        principal_id=genuine_uid_cgroup_decision.principal_id,
        kind=genuine_uid_cgroup_decision.kind,
        permitted=genuine_uid_cgroup_decision.permitted,
        execution_class="same_process",  # downgraded from uid_cgroup
        token=genuine_uid_cgroup_decision.token,  # token minted for uid_cgroup, unchanged
    )
    downgrade_verifies = gate.verify(downgraded_decision)
    downgrade_refused = False
    try:
        executor.execute(downgraded_decision, downgrade_intent)
    except GateViolation:
        downgrade_refused = True

    results["predicate_f_downgrade_replay_refused"] = {
        "gate_verify_result": downgrade_verifies,
        "executor_refused": downgrade_refused,
        "downgrade_target_file_absent": not downgrade_path.exists(),
    }

    return results


def main() -> int:
    require_real_root_linux()

    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_mediated_uid_cgroup_write"]
    b = results["predicate_b_distinct_uid_ground_truth"]
    c = results["predicate_c_cgroup_membership_while_running"]
    d = results["predicate_d_clean_release"]
    e = results["predicate_e_forged_refused_no_side_effects"]
    f = results["predicate_f_downgrade_replay_refused"]

    checks = [
        ("uid_cgroup assigned for privileged consequence", a["execution_class_assigned"] == "uid_cgroup"),
        ("mediated file content matches", a["file_content_matches"] is True),
        ("provisioned uid differs from broker uid", b["provisioned_uid_differs_from_broker"] is True),
        ("kernel ground truth matches provisioned uid", b["kernel_ground_truth_matches_provisioned_uid"] is True),
        ("kernel ground truth matches child self-report", b["kernel_ground_truth_matches_child_self_report"] is True),
        ("child confirmed alive at membership check", c["still_alive_at_check"] is True),
        ("child pid confirmed in cgroup.procs while blocked", c["child_pid_in_cgroup_while_blocked"] is True),
        ("cgroup released after execution", d["cgroup_released"] is True),
        ("ephemeral user released after execution", d["user_released"] is True),
        ("forged uid_cgroup decision refused", e["refused"] is True),
        ("forged file absent", e["forged_file_absent"] is True),
        ("forged attempt provisioned no users", e["no_new_users_provisioned"] is True),
        ("forged attempt provisioned no cgroups", e["no_new_cgroups_provisioned"] is True),
        ("downgrade: Gate.verify() returns False", f["gate_verify_result"] is False),
        ("downgrade: Executor refuses", f["executor_refused"] is True),
        ("downgrade target file absent", f["downgrade_target_file_absent"] is True),
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
