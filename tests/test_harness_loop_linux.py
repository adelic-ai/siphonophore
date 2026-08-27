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

B's delegated effect now goes through the real public `Broker.dispatch(intent, authority=...)`
interface, not `Gate.submit()`/`Executor.execute()` stitched together by hand -- a caller
demonstrating delegation no longer needs to know `Gate`/`Executor` exist at all. Real multi-loop
orchestration of a second, independently running agent is still separate, later integration work;
this closes the narrower gap where `Broker` itself couldn't express an authority-aware dispatch."""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.execution_uid_cgroup import ProvisioningError, UidCgroupBackend, require_real_root_linux
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker

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
    broker = Broker(gate=gate, executor=executor)

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

    # B exercises that authority for real, through the same public interface any ordinary
    # (non-delegated) caller uses -- a genuine attempted effect, checked against B's own delegated
    # scope, landing in the real uid+cgroup boundary. No direct Gate/Executor calls from here on.
    code = "import json,os,sys; print(json.dumps({'uid': os.getuid()}))"
    sub_intent = Intent(
        kind="run_artifact", principal_id="agent-a.sub-agent-b", intent_id="deleg-vertical-001",
        consequence="privileged", artifact_code=code,
    )
    effect = broker.dispatch(sub_intent, authority=authority_b)
    assert effect.execution_class == "uid_cgroup"  # policy-derived, independent of authority entirely

    obs = effect.detail["observations"]
    assert obs["real_uid_from_proc_status"] == obs["provisioned_uid"]
    assert obs["cgroup_released"] is True and obs["user_released"] is True
    report = json.loads(obs["stdout"])
    assert report["uid"] == obs["provisioned_uid"]


@requires_root_linux
def test_b_cannot_exercise_a_kind_outside_its_delegated_scope():
    """Property: the effect requested must be within the delegated authority -- not just that some
    delegation exists. A grants only run_artifact; B attempts write_file. Refused through the same
    public Broker.dispatch() interface the positive case above uses -- Gate mints a real, signed
    permitted=False Decision internally (not an exception at mint time), and Executor.execute()'s
    existing, unmodified check turns that into the GateViolation dispatch() propagates."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    broker = Broker(gate=gate, executor=Executor(gate))
    order = gate.issue_order("order-scope-001", "operator:ops-alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    out_of_scope = Intent(kind="write_file", principal_id="agent-a.sub-agent-b", intent_id="deleg-scope-001", consequence="low")
    with pytest.raises(GateViolation):
        broker.dispatch(out_of_scope, authority=authority_b)
