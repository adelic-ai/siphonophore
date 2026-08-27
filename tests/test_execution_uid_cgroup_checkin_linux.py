"""linux_root_only: CheckedInUidCgroupBackend exercised for real on colima -- real check-in over a
real Unix socket with SO_PEERCRED, real automatic reconciliation against a real delegate that lies
about one file and performs an undisclosed extra write (mirroring lab/007's scenario, but now
produced automatically by the backend rather than assembled by hand), and a real check-in timeout
treated as an identity failure with cleanup still running on every exit path."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.execution_uid_cgroup import ProvisioningError, require_real_root_linux
from siphonophore_core.execution_uid_cgroup_checkin import CheckedInUidCgroupBackend
from siphonophore_core.identity import IdentityError
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


requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason="needs real root on real Linux with cgroup v2 (run on colima)")

# Distinct range from every other siphonophore_core test file's range and every lab experiment's.
UID_MIN = 63700
UID_MAX = 63799

# Same four-case shape as lab/007 and test_audit_linux.py's lying delegate, but this time the
# check-in handshake is injected automatically by the backend -- this artifact_code only needs to
# perform its own effects and print its own self-report, exactly as any real delegate would.
_LYING_DELEGATE_CODE = """
import json, os, sys

payload = json.loads(sys.argv[1])
outdir = payload["outdir"]

with open(os.path.join(outdir, "corroborated.txt"), "w") as f:
    f.write("this write really happened and matches the claim")

with open(os.path.join(outdir, "contradicted.txt"), "w") as f:
    f.write("the real content, which differs from what the self-report is about to claim")

with open(os.path.join(outdir, "unreported.txt"), "w") as f:
    f.write("a real effect that happened but was never self-reported")

self_report = {
    "principal_id": "sub-agent",
    "claims": [
        {"path": "corroborated.txt", "content": "this write really happened and matches the claim"},
        {"path": "contradicted.txt", "content": "a claimed content that is simply not true"},
    ],
}
print(json.dumps(self_report))
"""

_NEVER_CHECKS_IN_WRAPPER = """
import time
time.sleep(30)
"""


@pytest.fixture
def backend():
    b = CheckedInUidCgroupBackend(uid_min=UID_MIN, uid_max=UID_MAX, checkin_timeout=1.5)
    yield b
    b.shutdown()


@pytest.fixture
def world_writable_outdir():
    d = Path(tempfile.mkdtemp(prefix="sipho-core-checkin-test-"))
    os.chmod(d, 0o777)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@requires_root_linux
def test_checked_in_delegation_with_automatic_reconciliation(backend: CheckedInUidCgroupBackend, world_writable_outdir: Path):
    gate = Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup_checkin"}))
    executor = Executor(gate, backends={"uid_cgroup_checkin": backend})

    intent = Intent(
        kind="run_artifact", principal_id="alice", intent_id="checkin-happy-01", consequence="privileged",
        payload={"outdir": str(world_writable_outdir)}, artifact_code=_LYING_DELEGATE_CODE,
    )
    decision = gate.submit(intent)
    assert decision.execution_class == "uid_cgroup_checkin"

    effect = executor.execute(decision, intent)
    obs = effect.detail["observations"]

    # real check-in, kernel-verified
    assert obs["checkin"]["verified"] is True
    assert obs["real_uid_from_proc_status"] == obs["provisioned_uid"]

    # automatic reconciliation, no manual audit.reconcile_path() call anywhere in this test
    reconciliation = obs["reconciliation"]
    assert reconciliation["corroborated.txt"]["value"] == "corroborated"
    assert reconciliation["contradicted.txt"]["value"] == "contradiction"
    assert reconciliation["unreported.txt"]["value"] == "unreported_activity"
    assert "never.txt" not in reconciliation  # never touched, never claimed -- not even mentioned

    assert obs["cgroup_released"] is True
    assert obs["user_released"] is True


@requires_root_linux
def test_delegate_with_no_self_report_gets_empty_self_report_not_a_hard_failure(backend: CheckedInUidCgroupBackend, world_writable_outdir: Path):
    """A delegate that performs a real effect but prints nothing parseable as a self-report is not
    treated as an execution failure -- self-report is optional (audit.py's own framing). The
    effect still reconciles correctly as unreported_activity."""
    code = """
import json, os, sys
payload = json.loads(sys.argv[1])
with open(os.path.join(payload["outdir"], "silent.txt"), "w") as f:
    f.write("did something, said nothing")
"""
    intent = Intent(
        kind="run_artifact", principal_id="alice", intent_id="checkin-silent-01", consequence="privileged",
        payload={"outdir": str(world_writable_outdir)}, artifact_code=code,
    )
    gate = Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup_checkin"}))
    executor = Executor(gate, backends={"uid_cgroup_checkin": backend})
    decision = gate.submit(intent)

    effect = executor.execute(decision, intent)
    obs = effect.detail["observations"]

    assert obs["self_report"]["claims"] == []
    assert obs["reconciliation"]["silent.txt"]["value"] == "unreported_activity"


@requires_root_linux
def test_checkin_timeout_is_an_identity_failure_and_still_releases_uid_and_cgroup(backend: CheckedInUidCgroupBackend):
    """A child that never performs check-in (here, by using a wrapper override that skips the
    handshake entirely) must not be trusted -- CheckedInUidCgroupBackend.run() raises
    CheckinFailedError (an IdentityError) after the backend's own checkin_timeout, and the
    provisioned uid/cgroup are still released.

    The raised exception must also carry the diagnostic trail (provisioned_uid, the check-in
    result, cleanup outcome) rather than discarding it as a bare message -- a caller catching a
    check-in failure still needs to know what was collected before attribution failed."""
    intent = Intent(
        kind="run_artifact", principal_id="alice", intent_id="checkin-timeout-01", consequence="privileged",
        payload={}, artifact_code="pass",
    )
    gate = Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup_checkin"}))
    decision = gate.submit(intent)

    with pytest.raises(IdentityError) as exc_info:
        backend.run(decision, intent, _child_wrapper=_NEVER_CHECKS_IN_WRAPPER)

    obs = exc_info.value.observations
    assert obs["provisioned_uid"] is not None
    assert obs["checkin"]["verified"] is False
    assert obs["checkin"]["reason"] == "timeout"
    assert obs["cgroup_released"] is True  # outer finally ran and mutated the same dict before catch
    assert obs["user_released"] is True

    import pwd

    username = f"sipho-core-{intent.intent_id[:8]}"
    with pytest.raises(KeyError):
        pwd.getpwnam(username)  # released, not leaked, despite the raised exception
    assert not (Path("/sys/fs/cgroup/siphonophore-core-checkin") / f"exec-{intent.intent_id}").exists()
