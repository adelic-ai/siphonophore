"""linux_root_only: the real Order -> Authority -> delegation -> Gate -> Executor ->
UidCgroupBackend vertical slice.

Supersedes an earlier version of this file, which only proved a "delegate"-kind Intent dispatched
through the identical Broker call as "run_artifact" -- offered as DESIGN.md section 7's
delegation-reduces-to-the-same-primitive proof. It wasn't: dispatching an inert kind string through
the same call proves repeated mediation, not delegated authority provenance (see HISTORY.md's
account of this project's own maturity assessment finding the gap). "delegate" is no longer an
Intent kind at all -- it's a category error to route a grant of authority through the same dispatch
path as an attempted effect. Real delegation is now `Gate.issue_order()` / `grant_root_authority()`
/ `delegate()` (authority.py), and this test drives that mechanism for real, landing the delegated
principal's effect in a genuine uid+cgroup boundary.

Deliberately bypasses `Broker`/`CognitiveLoop` for the authority-exercising step: `Broker.dispatch()`
doesn't accept an `authority` argument (it wasn't extended to -- see DESIGN.md's "explicitly open"
notes), so this composes `Gate.submit(intent, authority=...)` and `Executor.execute()` directly.
Harness-level (Broker/CognitiveLoop) exposure of authority-aware dispatch, and real multi-loop
orchestration of a second agent, are separate, later integration work, not part of this slice."""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.execution_uid_cgroup import ProvisioningError, UidCgroupBackend, require_real_root_linux
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, GateViolation
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
UID_MIN = 62900
UID_MAX = 62999


@requires_root_linux
def test_a_delegates_constrained_authority_to_b_who_executes_via_uid_cgroup():
    gate = Gate(ConsequencePolicy())
    executor = Executor(gate)
    executor.register_backend("uid_cgroup", UidCgroupBackend(uid_min=UID_MIN, uid_max=UID_MAX))

    # The originating order -- ungrounded, asserted by whatever issued it, exactly like
    # Intent.consequence already is (policy.py's own disclosed limitation, unchanged here).
    order = gate.issue_order(
        order_id="order-ops-ticket-001", issuer="operator:ops-alice",
        granted_kinds=frozenset({"run_artifact"}), max_delegation_depth=2,
    )

    # Principal A's own root authority, derived from the order.
    authority_a = gate.grant_root_authority(order, principal_id="agent-a")

    # A delegates a narrower authority to B -- same kinds, one hop of remaining depth consumed.
    authority_b = gate.delegate(authority_a, to_principal_id="agent-a.sub-agent-b")
    assert authority_b.parent_authority_id == authority_a.authority_id
    assert authority_b.order_id == order.order_id
    assert authority_b.scope.remaining_delegation_depth == authority_a.scope.remaining_delegation_depth - 1

    # B exercises that authority for real -- a genuine attempted effect, checked against B's own
    # delegated scope, landing in the real uid+cgroup boundary.
    code = "import json,os,sys; print(json.dumps({'uid': os.getuid()}))"
    sub_intent = Intent(
        kind="run_artifact", principal_id="agent-a.sub-agent-b", intent_id="deleg-vertical-001",
        consequence="privileged", artifact_code=code,
    )
    decision = gate.submit(sub_intent, authority=authority_b)
    assert decision.permitted is True
    assert decision.authority_id == authority_b.authority_id
    assert decision.order_id == order.order_id
    assert decision.execution_class == "uid_cgroup"  # policy-derived, independent of authority entirely

    effect = executor.execute(decision, sub_intent)

    obs = effect.detail["observations"]
    assert obs["real_uid_from_proc_status"] == obs["provisioned_uid"]
    assert obs["cgroup_released"] is True and obs["user_released"] is True
    report = json.loads(obs["stdout"])
    assert report["uid"] == obs["provisioned_uid"]


@requires_root_linux
def test_b_cannot_exercise_a_kind_outside_its_delegated_scope():
    """Property: the effect requested must be within the delegated authority -- not just that some
    delegation exists. A grants only run_artifact; B attempts write_file."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    order = gate.issue_order("order-scope-001", "operator:ops-alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    out_of_scope = Intent(kind="write_file", principal_id="agent-a.sub-agent-b", intent_id="deleg-scope-001", consequence="low")
    decision = gate.submit(out_of_scope, authority=authority_b)
    assert decision.permitted is False  # a real, signed refusal -- not an exception at mint time
    with pytest.raises(GateViolation):
        Executor(gate).execute(decision, out_of_scope)
