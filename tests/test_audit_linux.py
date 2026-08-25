"""linux_root_only: proves reconcile_path() against genuine ground truth from a real, delegated
sub-agent running under its own provisioned uid -- matching lab/007's real-delegation predicate.
The sub-agent's self-report actively lies about one file's content (a real T/F contradiction) and
performs an extra, undisclosed write (a real F/T unreported activity), alongside a genuinely
corroborated write (T/T) and a path neither touched nor claimed (F/F) -- so reconcile_path is shown
to distinguish all four cases from one real execution's data, not synthetic booleans."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from siphonophore_core.audit import SelfReport, collect_ground_truth, reconcile_path
from siphonophore_core.execution_uid_cgroup import (
    ProvisioningError,
    add_pid_to_cgroup,
    provision_cgroup,
    provision_ephemeral_user,
    release_cgroup,
    release_ephemeral_user,
    require_real_root_linux,
)

pytestmark = pytest.mark.linux_root_only


def _preconditions_met() -> bool:
    try:
        require_real_root_linux()
    except ProvisioningError:
        return False
    return True


requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason="needs real root on real Linux with cgroup v2 (run on colima)")

# Distinct range from every other siphonophore_core test file's range and every lab experiment's.
UID_MIN = 62700
UID_MAX = 62799
CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-core-audit-tests")

# Deliberately constructs, in ONE real execution, all four situations reconcile_path must tell
# apart: a genuine, truthful write (T/T); a claim about a file whose actual content differs from
# what's claimed -- a real, active lie (T/F); an extra write the self-report never mentions at all
# (F/T); and a path that is neither touched nor claimed (F/F, checked by the test but never
# referenced by this program at all).
_LYING_SUBAGENT_PROGRAM = """
import json, os, sys

outdir = sys.argv[1]

with open(os.path.join(outdir, "corroborated.txt"), "w") as f:
    f.write("this write really happened and matches the claim")

with open(os.path.join(outdir, "contradicted.txt"), "w") as f:
    f.write("the real content, which differs from what the self-report is about to claim")

with open(os.path.join(outdir, "unreported.txt"), "w") as f:
    f.write("a real effect that happened but was never self-reported")

# never.txt: deliberately neither touched nor claimed.

self_report = {
    "principal_id": "sub-agent",
    "claims": [
        {"path": "corroborated.txt", "content": "this write really happened and matches the claim"},
        {"path": "contradicted.txt", "content": "a claimed content that is simply not true"},
    ],
}
print(json.dumps(self_report))
"""


@pytest.fixture
def world_writable_outdir():
    d = Path(tempfile.mkdtemp(prefix="sipho-core-audit-test-"))
    os.chmod(d, 0o777)
    yield d
    shutil.rmtree(d, ignore_errors=True)


@requires_root_linux
def test_reconcile_path_against_real_lying_delegate(world_writable_outdir: Path):
    execution_id = "audit-lying-01"
    username, uid, gid = provision_ephemeral_user(execution_id, UID_MIN, UID_MAX)
    cgroup_path = provision_cgroup(CGROUP_ROOT, execution_id)

    stdout_fd, stdout_path = tempfile.mkstemp(prefix="sipho-core-audit-stdout-")
    proc: subprocess.Popen | None = None
    try:
        try:
            # user=/group=, not preexec_fn; file-redirected stdout, not PIPE/communicate() --
            # both patterns lab/006 found necessary under concurrent load, applied uniformly here
            # even though this dispatches a single delegation.
            proc = subprocess.Popen(
                [sys.executable, "-c", _LYING_SUBAGENT_PROGRAM, str(world_writable_outdir)],
                user=uid, group=gid, extra_groups=[],
                stdout=stdout_fd, stderr=subprocess.DEVNULL,
            )
            os.close(stdout_fd)
            add_pid_to_cgroup(cgroup_path, proc.pid)

            proc.wait(timeout=10)  # independently confirm exit BEFORE reading ground truth
            child_confirmed_exited = proc.poll() is not None
            child_returncode = proc.returncode

            self_report_text = Path(stdout_path).read_text().strip()
            self_report = SelfReport.from_dict(json.loads(self_report_text))

            ground_truth = collect_ground_truth(world_writable_outdir)
        finally:
            try:
                os.close(stdout_fd)
            except OSError:
                pass
            try:
                os.unlink(stdout_path)
            except OSError:
                pass
    finally:
        release_cgroup(cgroup_path)
        cgroup_released = not cgroup_path.exists()
        release_ephemeral_user(username)
        try:
            import pwd

            pwd.getpwnam(username)
            user_released = False
        except KeyError:
            user_released = True

    assert child_confirmed_exited is True
    assert child_returncode == 0

    corroborated = reconcile_path(self_report, "corroborated.txt",
                                   "this write really happened and matches the claim", ground_truth)
    contradicted = reconcile_path(self_report, "contradicted.txt",
                                   "a claimed content that is simply not true", ground_truth)
    unreported = reconcile_path(self_report, "unreported.txt", None, ground_truth)
    never = reconcile_path(self_report, "never.txt", None, ground_truth)

    assert corroborated["value"] == "corroborated"
    assert contradicted["value"] == "contradiction"
    assert unreported["value"] == "unreported_activity"
    assert never["value"] == "no_evidence"

    assert cgroup_released is True
    assert user_released is True
