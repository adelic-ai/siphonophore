"""linux_root_only: proves default_child_env() actually closes the gap it exists for -- a real
secret set in the broker's own environment must NOT be visible to a real spawned child, even
though env=None (the default) is passed nowhere explicit and the child runs under a genuinely
different, dropped-privilege uid. Mirrors the exact bug shape "Trusted Enough to Run" (Black Hat
USA 2026) documents in Gemini CLI: a real OS-level channel (there, /proc/<pid>/environ under a
shared PID namespace; here, subprocess.Popen()'s default full-inheritance behavior) that an
application-layer control never actually closed. Before this fix, both backends below would have
handed the child the broker's full environment; this test would have failed."""
from __future__ import annotations

import json
import os

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.execution_uid_cgroup import ProvisioningError, UidCgroupBackend, require_real_root_linux
from siphonophore_core.execution_uid_cgroup_checkin import CheckedInUidCgroupBackend
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

UID_MIN = 63900
UID_MAX = 63999

_SECRET_KEY = "SIPHONOPHORE_TEST_FAKE_SECRET"
_SECRET_VALUE = "sk-fake-secret-must-not-leak-to-child"

_PROBE_CODE = "import json, os, sys; print(json.dumps(dict(os.environ)))"


@pytest.fixture(autouse=True)
def _fake_secret_in_broker_env(monkeypatch):
    monkeypatch.setenv(_SECRET_KEY, _SECRET_VALUE)
    assert os.environ[_SECRET_KEY] == _SECRET_VALUE  # confirm the broker process really has it


@requires_root_linux
def test_uid_cgroup_child_does_not_inherit_the_brokers_secret():
    gate = Gate(ConsequencePolicy())
    executor = Executor(gate)
    executor.register_backend("uid_cgroup", UidCgroupBackend(uid_min=UID_MIN, uid_max=UID_MAX))

    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="env-leak-uid-cgroup", consequence="privileged", artifact_code=_PROBE_CODE)
    decision = gate.submit(intent)
    effect = executor.execute(decision, intent)

    child_env = json.loads(effect.detail["observations"]["stdout"])
    assert _SECRET_KEY not in child_env


@requires_root_linux
def test_checked_in_uid_cgroup_child_does_not_inherit_the_brokers_secret():
    gate = Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup_checkin"}))
    backend = CheckedInUidCgroupBackend(uid_min=UID_MIN, uid_max=UID_MAX, checkin_timeout=5.0)
    try:
        executor = Executor(gate, backends={"uid_cgroup_checkin": backend})
        intent = Intent(kind="run_artifact", principal_id="alice", intent_id="env-leak-checkin", consequence="privileged", artifact_code=_PROBE_CODE)
        decision = gate.submit(intent)
        effect = executor.execute(decision, intent)

        child_env = json.loads(effect.detail["observations"]["stdout"])
        assert _SECRET_KEY not in child_env
    finally:
        backend.shutdown()
