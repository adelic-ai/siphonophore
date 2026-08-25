"""Tests for UidCgroupBackend. linux_root_only: needs real root on real Linux (useradd/userdel,
cgroup v2) -- run these on colima, per every lab/004-009 experiment's own discipline. Skipped
automatically anywhere the preconditions aren't real (never silently degraded -- either it runs
for real or it's skipped, matching require_real_root_linux()'s own fail-loud stance)."""
from __future__ import annotations

from pathlib import Path

import pytest

from siphonophore_core.execution import ArtifactMismatchError, ExecutionError, Executor
from siphonophore_core.execution_uid_cgroup import (
    ProvisioningError,
    UidCgroupBackend,
    require_real_root_linux,
)
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy

pytestmark = pytest.mark.linux_root_only


def _preconditions_met() -> bool:
    try:
        require_real_root_linux()
    except ProvisioningError:
        return False
    return True


skip_reason = "needs real root on real Linux with cgroup v2 (run on colima)"
requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason=skip_reason)


# Distinct range from every lab experiment and from execution_uid_cgroup.py's own default, so a
# test run can never collide with leftover state from a lab script or a prior manual run.
TEST_UID_MIN = 61500
TEST_UID_MAX = 61599
TEST_CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-core-tests")


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup"}))


@pytest.fixture
def executor(gate: Gate) -> Executor:
    ex = Executor(gate)
    ex.register_backend("uid_cgroup", UidCgroupBackend(uid_min=TEST_UID_MIN, uid_max=TEST_UID_MAX, cgroup_root=TEST_CGROUP_ROOT))
    return ex


@requires_root_linux
def test_uid_cgroup_runs_under_a_real_ephemeral_uid_and_releases_everything(executor: Executor):
    code = "import json,os,sys; print(json.dumps({'uid': os.getuid()}))"
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-real-1", consequence="privileged", artifact_code=code)
    decision = executor._gate.submit(intent)
    assert decision.execution_class == "uid_cgroup"

    effect = executor.execute(decision, intent)
    obs = effect.detail["observations"]

    assert TEST_UID_MIN <= obs["provisioned_uid"] <= TEST_UID_MAX
    assert obs["provisioned_uid"] != 0  # never actually root
    assert obs["real_uid_from_proc_status"] == obs["provisioned_uid"]  # kernel-verified, not self-reported
    assert obs["cgroup_members_while_blocked"] != []  # real membership observed while process was alive
    assert obs["child_returncode"] == 0

    import json

    reported = json.loads(obs["stdout"])
    assert reported["uid"] == obs["provisioned_uid"]  # child's own view of itself matches ground truth

    # release on the success path
    assert obs["cgroup_released"] is True
    assert obs["user_released"] is True


@requires_root_linux
def test_uid_cgroup_swapped_artifact_is_refused_before_any_real_provisioning(executor: Executor):
    """ArtifactMismatchError is raised by Executor before the backend's run() is ever called --
    confirms (like lab/009) that a rejected swap costs zero real useradd/cgroup provisioning."""
    from dataclasses import replace

    real_intent = Intent(
        kind="run_artifact", principal_id="alice", intent_id="i-real-2", consequence="privileged",
        artifact_code="import sys; sys.exit(0)",
    )
    decision = executor._gate.submit(real_intent)
    swapped = replace(real_intent, artifact_code="import sys; sys.exit(1)")

    users_before = _list_sipho_users()
    with pytest.raises(ArtifactMismatchError):
        executor.execute(decision, swapped)
    assert _list_sipho_users() == users_before  # no real user was ever provisioned


@requires_root_linux
def test_uid_cgroup_failing_artifact_still_releases_uid_and_cgroup(executor: Executor):
    """A real failure inside the child (nonzero exit) must not leak the ephemeral user/cgroup --
    this is exactly the exception-unsafe gap lab/004 named and left open, lab/009 fixed."""
    code = "import sys; sys.exit(7)"
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-real-3", consequence="privileged", artifact_code=code)
    decision = executor._gate.submit(intent)

    with pytest.raises(ExecutionError):
        executor.execute(decision, intent)

    # the backend's own release-on-every-exit-path logic ran even though run() raised
    assert not (TEST_CGROUP_ROOT / "exec-i-real-3").exists()
    assert "sipho-core-i-real-3" not in _list_sipho_users()


def _list_sipho_users() -> set[str]:
    import pwd

    return {pw.pw_name for pw in pwd.getpwall() if pw.pw_name.startswith("sipho-core-")}
