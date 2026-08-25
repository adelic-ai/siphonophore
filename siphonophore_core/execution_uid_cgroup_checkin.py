"""CheckedInUidCgroupBackend -- uid_cgroup execution gated by a real check-in, with automatic
Belnap reconciliation (DESIGN.md sections 2, 3, 8; lab/005, lab/006, lab/007). The first place
identity.py's check-in protocol and audit.py's reconciliation are wired into an actual Executor
backend, rather than staying validated-but-freestanding primitives.

Registered under its own execution_class, "uid_cgroup_checkin", instead of folded into
UidCgroupBackend itself -- per DESIGN.md section 2, execution class follows authority, not
capability type, so which class an intent gets is a Policy decision (policy.py), never a special
case keyed on intent.kind. UidCgroupBackend's own simpler, unchecked trust model (lab/004: trusted
the instant it is spawned) stays available and unmodified under "uid_cgroup"; this backend is an
additional, selectable tier, not a replacement.

Two things UidCgroupBackend does not do:

1. The spawned process must prove it is the one this backend actually provisioned -- a nonce
   delivered over an inherited pipe fd (never argv) plus SO_PEERCRED (the kernel's own record of
   the connecting uid) -- before the backend treats anything the process did as attributable. A
   check-in failure or timeout is treated as an identity failure (`identity.IdentityError`), not a
   silently-trusted effect. One shared `CheckinListener` serves every dispatch through a given
   backend instance (lab/006's design, not lab/005's simpler dedicated-socket-per-delegation
   shape) -- callers that construct this backend own its lifetime and must call `shutdown()` when
   done with it, the same contract lab/006's own `Executor.shutdown()` had.
2. If the caller supplies an "outdir" key in `intent.payload`, the child's stdout is parsed as a
   self-report (`audit.SelfReport`) and automatically reconciled against ground truth collected
   from that directory (`audit.collect_ground_truth`/`reconcile_path`) -- lab/007's logic, run
   automatically rather than only when a caller remembers to invoke it by hand. Reconciliation is
   informational, attached to the Effect's detail; this backend never uses it to block or retry an
   execution that has already passed check-in -- deciding what to do about a contradiction or
   unreported activity is a policy question DESIGN.md section 4 leaves to the caller, scaled to
   what is actually at stake.

Caller-supplied `intent.artifact_code` targeting this backend receives `intent.payload` as a single
JSON-encoded `sys.argv[1]` (the same convention `execution.py`'s `SeparateProcessBackend` uses) and,
if it wants reconciliation to mean anything, should print a single JSON object to stdout shaped
like `audit.SelfReport.from_dict()` expects: `{"principal_id": ..., "claims": [{"path": ...,
"content": ...}, ...]}`. If stdout is not parseable as that shape, self-report is treated as empty
(no claims) rather than failing the execution -- matching audit.py's own framing that self-report
is optional and "worthless as ground truth on its own".
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .audit import SelfReport, collect_ground_truth, reconcile_path
from .execution import ExecutionBackend, ExecutionError
from .execution_uid_cgroup import (
    ProvisioningError,
    add_pid_to_cgroup,
    provision_cgroup,
    provision_ephemeral_user,
    read_real_uid_from_proc,
    release_cgroup,
    release_ephemeral_user,
    require_real_root_linux,
)
from .identity import CheckinListener, CheckinRegistry, IdentityError, generate_nonce, nonce_pipe
from .intent import Effect, Intent
from .policy import Decision

_CHECKIN_CHILD_WRAPPER = """
import sys
from siphonophore_core.identity import perform_checkin, read_nonce_from_fd

payload_json, socket_path, nonce_fd = sys.argv[1], sys.argv[2], int(sys.argv[3])
nonce = read_nonce_from_fd(nonce_fd)
if not perform_checkin(socket_path, nonce):
    sys.exit(97)
{body}
"""


class CheckinFailedError(IdentityError):
    """A delegated execution failed or timed out its check-in. Carries `observations` -- every
    diagnostic field collected before the failure (provisioned uid, kernel-verified uid from
    /proc, the check-in result itself, and any output the child produced before being killed) --
    so a caller catching this doesn't lose that trail the way a bare exception message would.

    What failed is attribution -- the binding between the identity the broker provisioned and the
    process claiming to be it -- not necessarily "nothing happened." In this backend's own wrapper
    (_CHECKIN_CHILD_WRAPPER) the child never reaches `intent.artifact_code` until after check-in
    succeeds, so no artifact-authored effect can occur before this exception is raised through the
    normal dispatch path -- but that is a property of this one wrapper shape, not a structural
    guarantee the architecture makes for every possible check-in-gated backend, and should not be
    assumed to hold for a different one without checking."""

    def __init__(self, message: str, observations: dict) -> None:
        super().__init__(message)
        self.observations = observations


class CheckedInUidCgroupBackend(ExecutionBackend):
    def __init__(self, uid_min: int = 63500, uid_max: int = 63599, cgroup_root: Path | None = None,
                 checkin_timeout: float = 10.0) -> None:
        require_real_root_linux()
        self._uid_min = uid_min
        self._uid_max = uid_max
        self._cgroup_root = cgroup_root or Path("/sys/fs/cgroup/siphonophore-core-checkin")
        self._checkin_timeout = checkin_timeout
        self._registry = CheckinRegistry()
        socket_path = f"/tmp/sipho-core-checkin-{uuid.uuid4().hex[:8]}.sock"
        self._listener = CheckinListener(socket_path, self._registry)
        self._listener.start()

    def shutdown(self) -> None:
        self._listener.stop()

    def run(self, decision: Decision, intent: Intent, *, _child_wrapper: str = _CHECKIN_CHILD_WRAPPER) -> Effect:
        if intent.artifact_code is None:
            raise ExecutionError("uid_cgroup_checkin backend requires intent.artifact_code")

        execution_id = decision.intent_id
        observations: dict = {}

        username, uid, gid = provision_ephemeral_user(execution_id, self._uid_min, self._uid_max)
        observations["provisioned_uid"] = uid
        cgroup_path = provision_cgroup(self._cgroup_root, execution_id)

        nonce = generate_nonce()
        self._registry.register_pending(execution_id, nonce, expected_uid=uid)
        read_fd, write_fd = nonce_pipe(nonce)

        def _drop_privileges() -> None:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

        proc: subprocess.Popen | None = None
        try:
            try:
                wrapped = _child_wrapper.format(body=intent.artifact_code)
                proc = subprocess.Popen(
                    [sys.executable, "-c", wrapped, json.dumps(intent.payload), self._listener.socket_path, str(read_fd)],
                    pass_fds=(read_fd,), preexec_fn=_drop_privileges,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                os.close(read_fd)
                os.close(write_fd)
                add_pid_to_cgroup(cgroup_path, proc.pid)
                observations["real_uid_from_proc_status"] = read_real_uid_from_proc(proc.pid)

                checkin_result = self._registry.wait_for_result(nonce, timeout=self._checkin_timeout)
                observations["checkin"] = checkin_result

                if not checkin_result.get("verified"):
                    if proc.poll() is None:
                        proc.kill()
                    try:
                        # communicate(), not a bare wait() -- captures whatever the child printed
                        # before being killed instead of silently discarding it. In this backend's
                        # own wrapper that is never artifact-authored output (check-in gates
                        # {body}), but the diagnostic is collected honestly rather than assumed
                        # empty.
                        stdout, stderr = proc.communicate(timeout=5)
                        observations["child_stdout_before_checkin_failure"] = stdout
                        observations["child_stderr_before_checkin_failure"] = stderr
                    except subprocess.TimeoutExpired:
                        pass
                    observations["child_returncode"] = proc.returncode
                    # Same dict object, not a copy -- the outer finally blocks below still mutate
                    # it (cgroup_released, user_released) as cleanup runs during unwind, and those
                    # mutations are visible via err.observations once the caller catches this,
                    # since the finally blocks run before the exception fully leaves this frame.
                    raise CheckinFailedError(
                        f"execution {execution_id!r} failed check-in: {checkin_result.get('reason')}",
                        observations=observations,
                    )

                stdout, stderr = proc.communicate(timeout=10)
                observations["child_returncode"] = proc.returncode
                if proc.returncode != 0:
                    raise ExecutionError(f"uid_cgroup_checkin artifact exited {proc.returncode}: {stderr}")
                observations["stdout"] = stdout

                self_report = _parse_self_report(stdout, default_principal_id=intent.principal_id)
                observations["self_report"] = {
                    "principal_id": self_report.principal_id,
                    "claims": [{"path": c.path, "content": c.content} for c in self_report.claims],
                }

                outdir = intent.payload.get("outdir") if isinstance(intent.payload, dict) else None
                if outdir is not None:
                    ground_truth = collect_ground_truth(Path(outdir))
                    observations["ground_truth"] = ground_truth
                    observations["reconciliation"] = _reconcile_all(self_report, ground_truth)
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
            # Release on EVERY exit path, including a check-in failure/timeout -- the same
            # discipline execution_uid_cgroup.py's UidCgroupBackend already applies.
            try:
                release_cgroup(cgroup_path)
                observations["cgroup_released"] = not cgroup_path.exists()
            except ProvisioningError:
                observations["cgroup_released"] = False
            release_ephemeral_user(username)
            observations["user_released"] = _user_is_released(username)

        return Effect(
            intent_id=intent.intent_id, execution_class="uid_cgroup_checkin",
            detail={"acting_pid": proc.pid if proc else None, "observations": observations},
        )


def _parse_self_report(stdout: str, *, default_principal_id: str) -> SelfReport:
    text = stdout.strip()
    if not text:
        return SelfReport(principal_id=default_principal_id)
    try:
        return SelfReport.from_dict(json.loads(text))
    except (json.JSONDecodeError, TypeError, KeyError):
        return SelfReport(principal_id=default_principal_id)


def _reconcile_all(self_report: SelfReport, ground_truth: dict[str, str]) -> dict[str, dict]:
    claimed_paths = {c.path for c in self_report.claims}
    all_paths = claimed_paths | set(ground_truth)
    reconciliation: dict[str, dict] = {}
    for path in all_paths:
        claim = next((c for c in self_report.claims if c.path == path), None)
        expected_content = claim.content if claim is not None else None
        reconciliation[path] = reconcile_path(self_report, path, expected_content, ground_truth)
    return reconciliation


def _user_is_released(username: str) -> bool:
    import pwd

    try:
        pwd.getpwnam(username)
        return False
    except KeyError:
        return True
