"""CheckedInSpawnHelperBackend -- composes the real check-in protocol (identity.py) and Belnap
reconciliation (audit.py) with the unprivileged-broker uid_cgroup path (execution_spawn_helper.py),
without modifying siphonophore-spawn.c, contracts/spawn_helper.md, or CheckedInUidCgroupBackend.

Nothing new was added to the privileged helper to make this possible -- SH-09/SH-24's optional
nonce field (fd 5) already exists in the pinned contract, unused until now. This backend generates
a real nonce, sends it through that existing channel, and wraps `intent.artifact_code` so the
artifact performs the identical check-in call (`identity.perform_checkin`) `CheckedInUidCgroupBackend`
already requires -- verified the same way, by the broker's own CheckinListener independently reading
SO_PEERCRED off the connecting socket, never by anything the artifact process merely asserts.

Registered under the SAME execution_class, "uid_cgroup_checkin", that CheckedInUidCgroupBackend
already uses -- a deployment choice (which implementation to register), not a new Gate/Executor/
Decision concept. `CheckedInUidCgroupBackend` itself is unmodified and still requires real root
(preexec_fn); this backend is the unprivileged-broker-compatible alternative for the identical
execution class, exactly mirroring how SpawnHelperBackend relates to UidCgroupBackend under
"uid_cgroup".

Reuses CheckinFailedError, _parse_self_report, and _reconcile_all directly from
execution_uid_cgroup_checkin.py rather than duplicating them -- that module stays structurally
unchanged; this one imports from it, the same way execution_spawn_helper.py already imports
_elevation_prefix from execution_uid_cgroup.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

from .audit import collect_ground_truth
from .execution import ExecutionBackend, ExecutionError
from .execution_spawn_helper import _elevation_prefix, _spawn_helper_path
from .execution_uid_cgroup import provision_ephemeral_user, release_ephemeral_user
from .execution_uid_cgroup_checkin import CheckinFailedError, _parse_self_report, _reconcile_all
from .identity import CheckinListener, CheckinRegistry, generate_nonce
from .intent import Effect, Intent
from .policy import Decision

# Unlike _CHECKIN_CHILD_WRAPPER (execution_uid_cgroup_checkin.py), which reads payload/socket_path/
# nonce_fd from argv -- the calling convention the preexec_fn path uses -- this wrapper targets
# bootstrap.py's calling convention: `payload` and `NONCE_FD` already arrive as globals (SH-24's
# fixed fd layout), so socket_path is the only thing that needs embedding directly into the source
# string, exactly like {body} already is.
_SPAWN_CHECKIN_WRAPPER = """
import sys
from siphonophore_core.identity import perform_checkin, read_nonce_from_fd

nonce = read_nonce_from_fd(NONCE_FD)
if not perform_checkin({socket_path!r}, nonce):
    sys.exit(97)
{body}
"""


class CheckedInSpawnHelperBackend(ExecutionBackend):
    def __init__(self, uid_min: int, uid_max: int, checkin_timeout: float = 10.0) -> None:
        self._uid_min = uid_min
        self._uid_max = uid_max
        self._checkin_timeout = checkin_timeout
        self._registry = CheckinRegistry()
        import uuid
        socket_path = f"/tmp/sipho-core-spawn-checkin-{uuid.uuid4().hex[:8]}.sock"
        self._listener = CheckinListener(socket_path, self._registry)
        self._listener.start()

    def shutdown(self) -> None:
        self._listener.stop()

    def run(self, decision: Decision, intent: Intent) -> Effect:
        if intent.artifact_code is None:
            raise ExecutionError("uid_cgroup_checkin (spawn helper) backend requires intent.artifact_code")

        execution_id = decision.intent_id
        observations: dict = {}

        username, uid, _gid = provision_ephemeral_user(execution_id, self._uid_min, self._uid_max)
        observations["provisioned_uid"] = uid

        try:
            nonce = generate_nonce()
            self._registry.register_pending(execution_id, nonce, expected_uid=uid)

            wrapped_source = _SPAWN_CHECKIN_WRAPPER.format(socket_path=self._listener.socket_path, body=intent.artifact_code)
            source_bytes = wrapped_source.encode("utf-8")
            payload_bytes = json.dumps(intent.payload).encode("utf-8")
            nonce_bytes = nonce.encode("utf-8")
            envelope = {
                "version": 1, "uid": uid, "username": username, "execution_id": execution_id,
                "code_length": len(source_bytes), "payload_length": len(payload_bytes),
                "nonce_length": len(nonce_bytes),
            }
            stream = json.dumps(envelope).encode("utf-8") + b"\n" + source_bytes + payload_bytes + nonce_bytes

            cmd = [*_elevation_prefix(), _spawn_helper_path()]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            # Feed stdin on a separate thread rather than a single blocking write() -- `stream` is
            # bounded by SH-14's own hardcoded caps but could still exceed the pipe buffer; writing
            # it inline here while also needing to wait_for_result() concurrently below is exactly
            # the write-stdin/read-stdout-while-doing-something-else shape subprocess's own docs
            # warn deadlocks on. communicate() avoids this internally but blocks until exit, which
            # would defeat waiting on check-in concurrently with the process still running.
            def _feed_stdin() -> None:
                try:
                    proc.stdin.write(stream)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            writer = threading.Thread(target=_feed_stdin, daemon=True)
            writer.start()

            checkin_result = self._registry.wait_for_result(nonce, timeout=self._checkin_timeout)
            observations["checkin"] = checkin_result
            writer.join(timeout=5)
            # The writer thread already closed the real stdin fd -- communicate() below still
            # tries to flush/close `self.stdin` itself unless told not to, and flushing an
            # already-closed file raises ValueError *inside* communicate(), before it finishes
            # waiting for/reaping the child. Clearing the attribute (not the fd, already closed)
            # tells communicate() to skip stdin handling entirely.
            proc.stdin = None

            if not checkin_result.get("verified"):
                if proc.poll() is None:
                    proc.kill()
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                    observations["child_stdout_before_checkin_failure"] = stdout.decode(errors="replace")
                    observations["child_stderr_before_checkin_failure"] = stderr.decode(errors="replace")
                except subprocess.TimeoutExpired:
                    pass
                observations["returncode"] = proc.returncode
                raise CheckinFailedError(
                    f"execution {execution_id!r} failed check-in: {checkin_result.get('reason')}",
                    observations=observations,
                )

            stdout, stderr = proc.communicate(timeout=10)
            observations["returncode"] = proc.returncode
            if proc.returncode != 0:
                raise ExecutionError(
                    f"siphonophore-spawn (checked-in) exited {proc.returncode}: {stderr.decode(errors='replace')}"
                )
            stdout_text = stdout.decode(errors="replace")
            observations["stdout"] = stdout_text

            self_report = _parse_self_report(stdout_text, default_principal_id=intent.principal_id)
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
            release_ephemeral_user(username)
            import pwd
            try:
                pwd.getpwnam(username)
                observations["user_released"] = False
            except KeyError:
                observations["user_released"] = True

        return Effect(
            intent_id=intent.intent_id, execution_class="uid_cgroup_checkin",
            detail={"observations": observations},
        )
