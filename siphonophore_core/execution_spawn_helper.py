"""SpawnHelperBackend -- the uid+cgroup ExecutionBackend that lets a genuinely UNPRIVILEGED broker
use the tier `UidCgroupBackend` (execution_uid_cgroup.py) requires real root for.

Delegates the actual privilege-requiring work entirely to `siphonophore-spawn`
(spawn_helper/siphonophore-spawn.c, contracts/spawn_helper.md, PINNED) -- this module is only the
client side: provision an identity, frame the envelope, invoke the helper via sudo, parse the
result. It does not, and must not, reimplement any of SH-01..26 itself; the helper alone owns
identity cross-validation, cgroup membership, environment sanitization, and privilege drop.

Cgroup leaves are NOT cleaned up by this backend. This is a deliberate, disclosed limitation, not
an oversight: cleanup would require either delegating CGROUP_ROOT to the broker (reopening the
independent-leaf-creation gap contracts/spawn_helper.md's SH-23 section already names as a limit
on what the helper can prove) or a separate broker-triggerable privileged removal path (which would
let a broker delete a finished execution's leaf and replay the same execution_id through the
helper again -- defeating SH-23's one-real-spawn-ever property, not just its concurrent-reuse
guarantee). Neither is worth the cost for what a low-weight kernfs leak actually costs in practice.
See HISTORY.md for the fuller reasoning.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .execution import ExecutionBackend, ExecutionError
from .execution_uid_cgroup import (
    _elevation_prefix,
    provision_ephemeral_user,
    release_ephemeral_user,
)
from .intent import Effect, Intent
from .policy import Decision

_SPAWN_HELPER_ENV = "SIPHONOPHORE_SPAWN_HELPER"
_DEFAULT_SPAWN_HELPER_PATH = "/usr/local/libexec/siphonophore-spawn"

# Matches spawn_helper/siphonophore-spawn.c's own hardcoded UID_MIN/UID_MAX exactly -- a uid this
# backend provisions outside that range would be refused by the helper's own SH-17 check
# regardless of anything this module does, so the default here is the helper's own compiled-in
# range, not an independent choice.
HELPER_UID_MIN = 60000
HELPER_UID_MAX = 65535


def _spawn_helper_path() -> str:
    return os.environ.get(_SPAWN_HELPER_ENV) or _DEFAULT_SPAWN_HELPER_PATH


class SpawnHelperBackend(ExecutionBackend):
    """`ExecutionBackend` for `uid_cgroup`, implemented entirely through `siphonophore-spawn`
    rather than `preexec_fn` -- register this instead of `UidCgroupBackend` when the broker itself
    must stay genuinely unprivileged. Both implement the identical `uid_cgroup` execution class;
    which one a caller registers is a deployment choice (DESIGN.md section 6: the executor/
    substrate backend is a customizable mechanism), not something Gate or Executor branch on.
    """

    def __init__(self, uid_min: int = HELPER_UID_MIN, uid_max: int = HELPER_UID_MAX) -> None:
        if uid_min < HELPER_UID_MIN or uid_max > HELPER_UID_MAX:
            raise ValueError(
                f"uid range [{uid_min}, {uid_max}] must stay within the spawn helper's own "
                f"compiled-in range [{HELPER_UID_MIN}, {HELPER_UID_MAX}] (siphonophore-spawn.c) -- "
                f"a uid outside that range would be refused by the helper's own SH-17 check "
                f"regardless of what this backend provisions"
            )
        self._uid_min = uid_min
        self._uid_max = uid_max

    def run(self, decision: Decision, intent: Intent) -> Effect:
        if intent.artifact_code is None:
            raise ExecutionError("uid_cgroup (spawn helper) backend requires intent.artifact_code")

        execution_id = decision.intent_id
        observations: dict = {}

        username, uid, _gid = provision_ephemeral_user(execution_id, self._uid_min, self._uid_max)
        observations["provisioned_uid"] = uid

        try:
            source_bytes = intent.artifact_code.encode("utf-8")
            payload_bytes = json.dumps(intent.payload).encode("utf-8")
            envelope = {
                "version": 1,
                "uid": uid,
                "username": username,
                "execution_id": execution_id,
                "code_length": len(source_bytes),
                "payload_length": len(payload_bytes),
                "nonce_length": 0,
            }
            stream = json.dumps(envelope).encode("utf-8") + b"\n" + source_bytes + payload_bytes

            cmd = [*_elevation_prefix(), _spawn_helper_path()]
            try:
                proc = subprocess.run(cmd, input=stream, capture_output=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise ExecutionError(f"siphonophore-spawn did not return within the timeout: {exc}") from exc

            observations["returncode"] = proc.returncode
            if proc.returncode != 0:
                raise ExecutionError(
                    f"siphonophore-spawn refused or failed (exit {proc.returncode}): "
                    f"{proc.stderr.decode(errors='replace').strip()}"
                )
            observations["stdout"] = proc.stdout.decode(errors="replace")
        finally:
            release_ephemeral_user(username)
            try:
                import pwd
                pwd.getpwnam(username)
                observations["user_released"] = False
            except KeyError:
                observations["user_released"] = True

        return Effect(
            intent_id=intent.intent_id, execution_class="uid_cgroup",
            detail={"observations": observations},
        )
