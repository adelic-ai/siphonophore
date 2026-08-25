"""linux_root_only: DESIGN.md section 7's delegation-reduces-to-the-same-primitive proof, at full
strength -- a "delegate" kind Intent, parsed from a scripted completion by the same CognitiveLoop
used for an ordinary tool call, dispatched through the identical Broker instance, landing in the
identical UidCgroupBackend (real useradd, real cgroup v2, real privilege drop) with zero
special-casing anywhere in loop.py, broker.py, or execution.py for the "delegate" kind."""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.execution_uid_cgroup import ProvisioningError, UidCgroupBackend, require_real_root_linux
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.model import ScriptedModel

pytestmark = pytest.mark.linux_root_only


def _preconditions_met() -> bool:
    try:
        require_real_root_linux()
    except ProvisioningError:
        return False
    return True


requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason="needs real root on real Linux with cgroup v2 (run on colima)")

# Distinct range from every other siphonophore_core test file's range and every lab experiment's.
UID_MIN = 62900
UID_MAX = 62999


@requires_root_linux
def test_tool_call_and_delegation_both_reach_uid_cgroup_via_the_identical_dispatch_call():
    gate = Gate(ConsequencePolicy())
    executor = Executor(gate)
    executor.register_backend("uid_cgroup", UidCgroupBackend(uid_min=UID_MIN, uid_max=UID_MAX))
    broker = Broker(gate=gate, executor=executor)

    code = "import json,os,sys; print(json.dumps({'uid': os.getuid()}))"
    tool_call_completion = json.dumps({"kind": "run_artifact", "consequence": "privileged", "artifact_code": code})
    delegate_completion = json.dumps({"kind": "delegate", "consequence": "privileged", "artifact_code": code})

    loop = CognitiveLoop(model=ScriptedModel([tool_call_completion, delegate_completion]), broker=broker, principal_id="alice")

    tool_effect = loop.step("run this")
    delegate_effect = loop.step("delegate this")

    assert tool_effect.execution_class == "uid_cgroup"
    assert delegate_effect.execution_class == "uid_cgroup"

    tool_obs = tool_effect.detail["observations"]
    delegate_obs = delegate_effect.detail["observations"]

    # Each execution independently provisioned a real uid and released it before the next one ran
    # (sequential, not concurrent dispatch here -- see test_execution_uid_cgroup.py and
    # test_identity_linux.py for genuine concurrent-identity coverage) -- so the second call is
    # free to reuse the first's now-released uid number. That reuse is expected, not a shared
    # identity: what matters is that both, independently, are real kernel-verified uids matching
    # what was actually provisioned for that one dispatch.
    assert tool_obs["real_uid_from_proc_status"] == tool_obs["provisioned_uid"]
    assert delegate_obs["real_uid_from_proc_status"] == delegate_obs["provisioned_uid"]
    assert tool_obs["cgroup_released"] is True and tool_obs["user_released"] is True
    assert delegate_obs["cgroup_released"] is True and delegate_obs["user_released"] is True

    tool_report = json.loads(tool_obs["stdout"])
    delegate_report = json.loads(delegate_obs["stdout"])
    assert tool_report["uid"] == tool_obs["provisioned_uid"]
    assert delegate_report["uid"] == delegate_obs["provisioned_uid"]
