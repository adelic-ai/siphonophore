"""linux_root_only: SpawnHelperBackend (execution_spawn_helper.py) -- the real, unprivileged-broker
uid_cgroup path through siphonophore-spawn, closing the third and last piece of the
broker-root-privilege gap named in DESIGN.md.

Every other linux_root_only test runs pytest itself as root and exercises root-requiring code
directly in that process. This file proves something stronger: that the broker's OWN Python
process never holds euid 0. It does that by actually running the dispatch code in a SEPARATE
subprocess under a genuinely unprivileged system user (`sudo -u <broker>`), with a real, narrow
sudoers grant -- the same shape used to manually validate useradd/userdel and siphonophore-spawn
earlier in this project, now automated.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from siphonophore_core.execution import ArtifactMismatchError, Executor
from siphonophore_core.execution_spawn_helper import SpawnHelperBackend
from siphonophore_core.execution_uid_cgroup import ProvisioningError, require_real_root_linux
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy

pytestmark = pytest.mark.linux_root_only

REPO_ROOT = Path(__file__).resolve().parent.parent
SPAWN_HELPER_DIR = REPO_ROOT / "spawn_helper"
SCRIPTS_DIR = REPO_ROOT / "scripts"
HELPER_BINARY = "/usr/local/libexec/siphonophore-spawn"

BROKER_USER = "sipho-broker-inttest"
SUDOERS_PATH = Path("/etc/sudoers.d/sipho-spawn-helper-inttest")

# Distinct from every other test file's uid range, and within siphonophore-spawn.c's own
# compiled-in [60000, 65535] bound (SpawnHelperBackend itself enforces this at construction).
UID_MIN = 64200
UID_MAX = 64299


def _preconditions_met() -> bool:
    try:
        require_real_root_linux()
    except ProvisioningError:
        return False
    return True


requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason="needs real root on real Linux with cgroup v2 (run on colima)")


@pytest.fixture(scope="module")
def built_and_installed():
    subprocess.run(["make", "-C", str(SPAWN_HELPER_DIR)], check=True, capture_output=True)
    subprocess.run(["make", "-C", str(SPAWN_HELPER_DIR), "install"], check=True, capture_output=True)
    assert Path(HELPER_BINARY).exists()


@pytest.fixture
def broker_user(built_and_installed):
    """A real, genuinely unprivileged system user standing in for "the broker," with a real,
    narrow sudoers grant covering exactly the useradd/userdel wrapper scripts and the
    argument-free siphonophore-spawn invocation -- not ALL. Installed and torn down for real,
    confirmed clean afterward, matching this project's own validation discipline."""
    subprocess.run(["useradd", "-m", "-s", "/bin/bash", BROKER_USER], check=True, capture_output=True)
    sudoers_content = textwrap.dedent(f"""\
        Cmnd_Alias SIPHO_INTTEST = {SCRIPTS_DIR}/siphonophore-useradd, {SCRIPTS_DIR}/siphonophore-userdel, {HELPER_BINARY} ""
        {BROKER_USER} ALL=(root) NOPASSWD: SIPHO_INTTEST
        """)
    SUDOERS_PATH.write_text(sudoers_content)
    SUDOERS_PATH.chmod(0o440)
    check = subprocess.run(["visudo", "-c"], capture_output=True, text=True)
    assert check.returncode == 0, f"sudoers file failed validation: {check.stdout} {check.stderr}"
    try:
        yield BROKER_USER
    finally:
        SUDOERS_PATH.unlink(missing_ok=True)
        subprocess.run(["userdel", "-r", BROKER_USER], capture_output=True)


_DRIVER_TEMPLATE = """
import json, os, sys
sys.path.insert(0, {repo_root!r})
from siphonophore_core.execution import Executor
from siphonophore_core.execution_spawn_helper import SpawnHelperBackend
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy

gate = Gate(ConsequencePolicy())
executor = Executor(gate, backends={{}})
executor.register_backend("uid_cgroup", SpawnHelperBackend(uid_min={uid_min}, uid_max={uid_max}))

artifact = "import json,os,sys; print(json.dumps({{'uid': os.getuid(), 'cgroup': open('/proc/self/cgroup').read().strip(), 'payload': payload}}))"
intent = Intent(kind="run_artifact", principal_id="alice", intent_id={intent_id!r}, consequence="privileged", artifact_code=artifact, payload={{"k": "v"}})
decision = gate.submit(intent)
effect = executor.execute(decision, intent)

result = {{
    "broker_euid": os.geteuid(),
    "broker_uid": os.getuid(),
    "execution_class": effect.execution_class,
    "observations": effect.detail["observations"],
}}
print("RESULT_JSON:" + json.dumps(result))
"""


@requires_root_linux
def test_unprivileged_broker_executes_via_spawn_helper(broker_user: str):
    # Unique per invocation, not a fixed literal -- SH-23's replay-prevention means a leftover
    # cgroup leaf from a PRIOR run of this test (this backend doesn't auto-clean leaves, a
    # disclosed limitation -- see DESIGN.md/execution_spawn_helper.py) would otherwise cause a
    # real, reproducible failure on the second run, not a flaky one.
    intent_id = f"spawnbackend-inttest-{uuid.uuid4().hex[:8]}"
    driver = _DRIVER_TEMPLATE.format(repo_root=str(REPO_ROOT), uid_min=UID_MIN, uid_max=UID_MAX, intent_id=intent_id)

    proc = subprocess.run(
        ["sudo", "-u", broker_user, sys.executable, "-c", driver],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"broker-side driver failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"

    result_line = next(line for line in proc.stdout.splitlines() if line.startswith("RESULT_JSON:"))
    result = json.loads(result_line[len("RESULT_JSON:"):])

    # The central claim: the broker's OWN process never held root.
    assert result["broker_euid"] != 0
    assert result["broker_uid"] != 0

    obs = result["observations"]
    assert result["execution_class"] == "uid_cgroup"
    assert obs["returncode"] == 0
    # The artifact ran under a DIFFERENT, real, ephemeral uid -- neither the broker's own uid nor 0.
    provisioned_uid = obs["provisioned_uid"]
    assert provisioned_uid != result["broker_uid"]
    assert provisioned_uid != 0
    assert UID_MIN <= provisioned_uid <= UID_MAX
    assert obs["user_released"] is True

    artifact_report = json.loads(obs["stdout"])
    assert artifact_report["uid"] == provisioned_uid
    assert f"exec-{intent_id}" in artifact_report["cgroup"]
    assert artifact_report["payload"] == {"k": "v"}


@requires_root_linux
def test_artifact_substitution_still_refused_before_helper_is_invoked():
    """The existing Decision/artifact-provenance verification (Executor.execute()) must still run,
    unmodified, before this backend is ever reached -- proves ArtifactMismatchError fires without
    ever invoking siphonophore-spawn, exactly as it does for every other backend. Doesn't need the
    unprivileged-subprocess machinery: this property holds by construction (Executor checks the
    digest before calling backend.run() at all), so it's exercised directly here as root for
    simplicity, not because privilege matters for this specific check."""
    gate = Gate(ConsequencePolicy())
    executor = Executor(gate, backends={"uid_cgroup": SpawnHelperBackend(uid_min=UID_MIN, uid_max=UID_MAX)})

    real_intent = Intent(kind="run_artifact", principal_id="alice", intent_id="spawnbackend-swap-001",
                          consequence="privileged", artifact_code="print('A')")
    decision = gate.submit(real_intent)
    from dataclasses import replace
    swapped_intent = replace(real_intent, artifact_code="print('B')")

    with pytest.raises(ArtifactMismatchError):
        executor.execute(decision, swapped_intent)
