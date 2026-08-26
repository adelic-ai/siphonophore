"""UidCgroupBackend -- the uid+cgroup ExecutionBackend (DESIGN.md section 2).

Provisions a real, ephemeral, no-login system user and a real cgroup v2 leaf directory per
execution, spawns the artifact under that dropped-privilege identity, confirms real cgroup
membership and kernel-verified uid while the process is confirmed alive, and releases both on
every exit path.

Needs real root on real Linux. Fails loudly and immediately anywhere else -- never silently
degrades to a weaker execution class.

Built entirely fresh here -- not imported from lab/004-009 or (per HISTORY.md's account of the one
time this was violated) from any prior architecture's identity.py. This is the first place the
pattern lab/004 established and lab/009 fixed becomes real, shared code instead of the ninth
(soon tenth) independent copy of it.
"""
from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

from .execution import ExecutionBackend, ExecutionError
from .intent import Effect, Intent
from .policy import Decision


class ProvisioningError(RuntimeError):
    """A privileged operation (useradd, userdel, cgroup create/destroy) failed for a reason other
    than the platform/privilege check in require_real_root_linux() -- e.g. the uid range is
    exhausted, or a cgroup still has live members when release is attempted."""


def require_real_root_linux() -> None:
    if sys.platform != "linux":
        raise ProvisioningError(
            f"uid_cgroup requires real Linux (useradd, cgroup v2, /proc/<pid>/status are "
            f"Linux-specific); detected sys.platform={sys.platform!r}"
        )
    if os.geteuid() != 0:
        raise ProvisioningError(f"uid_cgroup requires real root; detected euid={os.geteuid()}")
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        raise ProvisioningError("uid_cgroup requires cgroup v2 (no cgroup.controllers file found)")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _find_free_uid(uid_min: int, uid_max: int) -> int:
    taken = {pw.pw_uid for pw in pwd.getpwall()}
    for candidate in range(uid_min, uid_max + 1):
        if candidate not in taken:
            return candidate
    raise ProvisioningError(f"no free uid in reserved range [{uid_min}, {uid_max}]")


def provision_ephemeral_user(execution_id: str, uid_min: int, uid_max: int) -> tuple[str, int, int]:
    uid = _find_free_uid(uid_min, uid_max)
    username = f"sipho-core-{execution_id[:8]}"
    result = _run([
        "useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--uid", str(uid),
        "--comment", "siphonophore-core ephemeral execution identity", username,
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


def provision_cgroup(cgroup_root: Path, execution_id: str) -> Path:
    cgroup_root.mkdir(parents=True, exist_ok=True)
    cg = cgroup_root / f"exec-{execution_id}"
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


# Environment variables safe to pass through to a spawned artifact by default -- deliberately NOT
# the parent's full environment. subprocess.Popen() inherits everything from the parent when `env`
# is omitted, which would hand a spawned artifact any secret the broker process itself holds (an
# API key, say) despite running under a genuinely separate, unprivileged uid -- the exact bug shape
# Elad Meged's "Trusted Enough to Run" (Black Hat USA 2026) documents in Gemini CLI: environment
# sanitization applied at the application layer while the OS-level channel (there, plain
# inheritance into a shared-PID-namespace child readable via /proc/<pid>/environ; here, plain
# inheritance into this child directly) was never closed. The uid separation here is real -- a
# genuinely different provisioned uid, confirmed via /proc/<pid>/status -- but until this fix, the
# environment boundary was never built at all, only the identity boundary was.
DEFAULT_CHILD_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


def default_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal, explicit-allowlist environment for a spawned artifact -- everything else in the
    broker's own environment is absent by construction, not filtered after the fact. `source`
    defaults to the real process environment; callers (tests, mainly) can pass an explicit dict so
    behavior doesn't depend on whatever happens to be set on the machine running them."""
    src = source if source is not None else os.environ
    return {key: src[key] for key in DEFAULT_CHILD_ENV_KEYS if key in src}


_CHILD_WRAPPER = """
import json, os, sys, time
payload_json, sync_fd = sys.argv[1], int(sys.argv[2])
os.read(sync_fd, 1)
time.sleep(0.05)
{body}
"""


class UidCgroupBackend(ExecutionBackend):
    """`ExecutionBackend` for `uid_cgroup`. Caller-configurable uid range and cgroup root so
    multiple call sites (or future backends) don't collide on the same reserved range -- every lab
    experiment from 004 onward picked its own distinct range for exactly this reason."""

    def __init__(self, uid_min: int = 60000, uid_max: int = 60999, cgroup_root: Path | None = None,
                 env: dict[str, str] | None = None) -> None:
        self._uid_min = uid_min
        self._uid_max = uid_max
        self._cgroup_root = cgroup_root or Path("/sys/fs/cgroup/siphonophore-core")
        # None (the default) means "compute default_child_env() fresh at run() time", not "inherit
        # everything" -- an explicit dict here (including {}) is used exactly as given.
        self._env = env

    def run(self, decision: Decision, intent: Intent) -> Effect:
        require_real_root_linux()
        if intent.artifact_code is None:
            raise ExecutionError("uid_cgroup backend requires intent.artifact_code")

        execution_id = decision.intent_id
        observations: dict = {}

        username, uid, gid = provision_ephemeral_user(execution_id, self._uid_min, self._uid_max)
        observations["provisioned_uid"] = uid
        cgroup_path = provision_cgroup(self._cgroup_root, execution_id)

        read_fd, write_fd = os.pipe()

        def _drop_privileges() -> None:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

        proc: subprocess.Popen | None = None
        try:
            try:
                wrapped = _CHILD_WRAPPER.format(body=intent.artifact_code)
                child_env = self._env if self._env is not None else default_child_env()
                proc = subprocess.Popen(
                    [sys.executable, "-c", wrapped, json.dumps(intent.payload), str(read_fd)],
                    pass_fds=(read_fd,),
                    preexec_fn=_drop_privileges,
                    env=child_env,
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
                    raise ExecutionError(f"uid_cgroup artifact exited {proc.returncode}: {stderr}")
                observations["stdout"] = stdout
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
            # Release on EVERY exit path -- lab/004 left this as a named, unfixed gap (release
            # logic only reachable on the success path); lab/009 fixed it by wrapping the whole
            # provision-through-dispatch sequence in one outer finally. Same fix, made permanent
            # here rather than needing to be rediscovered by whatever uses this backend next.
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

        return Effect(
            intent_id=intent.intent_id, execution_class="uid_cgroup",
            detail={"acting_pid": proc.pid if proc else None, "observations": observations},
        )
