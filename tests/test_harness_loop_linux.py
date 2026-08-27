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
demonstrating delegation no longer needs to know `Gate`/`Executor` exist at all.

Also composes the real check-in protocol (identity.py) and Belnap reconciliation (audit.py) into
this same delegated path via `CheckedInSpawnHelperBackend` -- delegated Authority -> public
`Broker.dispatch()` -> Gate re-verification -> real uid+cgroup identity through
`siphonophore-spawn` -> independently, kernel-verified check-in (SO_PEERCRED) -> a reconciliation
result tied to that specific execution. Nothing about `siphonophore-spawn.c`,
`contracts/spawn_helper.md`, or `CheckedInUidCgroupBackend` was touched to make this possible --
see `execution_spawn_helper_checkin.py`'s own docstring for exactly what was reused unchanged.

Finally, real multi-loop orchestration: two independently running `CognitiveLoop` instances, each
with its own `Model`/history/principal_id, sharing one `Gate`/`Executor`/`Broker`. Loop A holds its
own root `Authority`; loop B holds an `Authority` delegated from A, constructed by test code
standing in for whatever orchestrates a real deployment's agents (`CognitiveLoop` itself never
grants or derives authority -- see `loop.py`'s own docstring for why holding one doesn't add a
second path to an effect). Loop B's own model-produced completion, not directly-constructed test
`Intent` objects, is what reaches `broker.dispatch(authority=...)` here -- this is what makes
delegation visibly agent-to-agent rather than a test actor exercising delegated authority by hand."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

from siphonophore_core.execution import Executor, SameProcessBackend
from siphonophore_core.execution_spawn_helper_checkin import CheckedInSpawnHelperBackend
from siphonophore_core.execution_uid_cgroup import ProvisioningError, UidCgroupBackend, require_real_root_linux
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, GateViolation
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


# ---- delegation composed with real check-in + Belnap reconciliation ------------------------

CHECKIN_UID_MIN = 64400
CHECKIN_UID_MAX = 64499

# payload arrives as a global (bootstrap.py's calling convention), not sys.argv[1] -- unlike
# execution_uid_cgroup_checkin.py's own artifact code, which targets the older preexec_fn/argv
# calling convention.
_HONEST_DELEGATE_CODE = """
import json, os

with open(os.path.join(payload["outdir"], "corroborated.txt"), "w") as f:
    f.write("this write really happened and matches the claim")

self_report = {
    "principal_id": "agent-a.sub-agent-b",
    "claims": [{"path": "corroborated.txt", "content": "this write really happened and matches the claim"}],
}
print(json.dumps(self_report))
"""

_LYING_DELEGATE_CODE = """
import json, os

with open(os.path.join(payload["outdir"], "contradicted.txt"), "w") as f:
    f.write("the real content, which differs from what the self-report is about to claim")

with open(os.path.join(payload["outdir"], "unreported.txt"), "w") as f:
    f.write("a real effect that happened but was never self-reported")

self_report = {
    "principal_id": "agent-a.sub-agent-b",
    "claims": [{"path": "contradicted.txt", "content": "a claimed content that is simply not true"}],
}
print(json.dumps(self_report))
"""


@pytest.fixture
def world_writable_outdir():
    d = Path(tempfile.mkdtemp(prefix="sipho-core-deleg-checkin-test-"))
    os.chmod(d, 0o777)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _delegated_checkin_setup():
    gate = Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup_checkin"}))
    backend = CheckedInSpawnHelperBackend(uid_min=CHECKIN_UID_MIN, uid_max=CHECKIN_UID_MAX, checkin_timeout=10.0)
    executor = Executor(gate, backends={"uid_cgroup_checkin": backend})
    broker = Broker(gate=gate, executor=executor)
    order = gate.issue_order(
        order_id="order-checkin-ticket-001", issuer="operator:ops-alice",
        granted_kinds=frozenset({"run_artifact"}), max_delegation_depth=2,
    )
    authority_a = gate.grant_root_authority(order, principal_id="agent-a")
    authority_b = gate.delegate(authority_a, to_principal_id="agent-a.sub-agent-b")
    return broker, backend, authority_b


@requires_root_linux
def test_delegated_effect_produces_independently_verified_and_reconciled_evidence(world_writable_outdir: Path):
    """The full composed chain: delegated Authority -> Broker.dispatch() -> Gate re-verification ->
    real uid+cgroup identity via siphonophore-spawn -> independently, kernel-verified check-in
    (SO_PEERCRED, not anything the artifact asserts) -> a reconciliation result tied to this
    specific execution_id, not a separate, unrelated check."""
    broker, backend, authority_b = _delegated_checkin_setup()
    try:
        sub_intent = Intent(
            # Unique per invocation -- CheckedInSpawnHelperBackend doesn't auto-clean cgroup
            # leaves (disclosed limitation), so a fixed id would fail on a second run that
            # reuses a leftover leaf from the first, not flakily but every time.
            kind="run_artifact", principal_id="agent-a.sub-agent-b",
            intent_id=f"honest-checkin-{uuid.uuid4().hex[:8]}",
            consequence="privileged", payload={"outdir": str(world_writable_outdir)},
            artifact_code=_HONEST_DELEGATE_CODE,
        )
        effect = broker.dispatch(sub_intent, authority=authority_b)
        assert effect.execution_class == "uid_cgroup_checkin"

        obs = effect.detail["observations"]
        # Independently sourced, kernel-grounded identity proof -- SO_PEERCRED, not self-asserted.
        assert obs["checkin"]["verified"] is True
        assert obs["checkin"]["execution_id"] == sub_intent.intent_id

        # Reconciliation tied to this execution's own real files, not asserted separately.
        reconciliation = obs["reconciliation"]
        assert reconciliation["corroborated.txt"]["value"] == "corroborated"
        assert obs["user_released"] is True
    finally:
        backend.shutdown()


@requires_root_linux
def test_fabricated_self_report_is_not_reconciled_as_confirmation(world_writable_outdir: Path):
    """Negative case: B's own real check-in still verifies (identity is real and correct), but its
    SELF-REPORT lies about what it did -- a claimed path whose content doesn't match ground truth,
    and a real effect it never mentions at all. Neither must reconcile as "corroborated": a
    fabricated or substituted claim must show up as contradiction, and an undisclosed real effect
    must show up as unreported_activity -- confirmation requires independently observed ground
    truth agreeing with the claim, not merely a successful check-in or a plausible-sounding
    self-report."""
    broker, backend, authority_b = _delegated_checkin_setup()
    try:
        sub_intent = Intent(
            kind="run_artifact", principal_id="agent-a.sub-agent-b",
            intent_id=f"lying-checkin-{uuid.uuid4().hex[:8]}",
            consequence="privileged", payload={"outdir": str(world_writable_outdir)},
            artifact_code=_LYING_DELEGATE_CODE,
        )
        effect = broker.dispatch(sub_intent, authority=authority_b)
        obs = effect.detail["observations"]

        # The identity check-in itself is genuinely real and correct -- lying happens one layer up,
        # in self-report, which is exactly the distinction DESIGN.md section 3 exists to preserve.
        assert obs["checkin"]["verified"] is True

        reconciliation = obs["reconciliation"]
        assert reconciliation["contradicted.txt"]["value"] == "contradiction"
        assert reconciliation["unreported.txt"]["value"] == "unreported_activity"
        assert reconciliation["contradicted.txt"]["value"] != "corroborated"
        assert reconciliation["unreported.txt"]["value"] != "corroborated"
    finally:
        backend.shutdown()


# ---- real multi-loop orchestration: two independently running CognitiveLoop instances --------

TWO_LOOP_UID_MIN = 64600
TWO_LOOP_UID_MAX = 64699

_AGENT_B_CODE = """
import json, os

with open(os.path.join(payload["outdir"], "b_did_this.txt"), "w") as f:
    f.write("agent B's own real effect")

self_report = {
    "principal_id": "agent-a.sub-agent-b",
    "claims": [{"path": "b_did_this.txt", "content": "agent B's own real effect"}],
}
print(json.dumps(self_report))
"""


@requires_root_linux
def test_two_real_cognitive_loops_agent_a_delegates_to_agent_b(world_writable_outdir: Path):
    """Two independently running CognitiveLoop instances, each its own Model/history/principal_id,
    sharing one Gate/Executor/Broker. Loop A dispatches with its own root Authority; loop B
    dispatches with an Authority delegated from A -- constructed by this test, standing in for
    whatever orchestrates a real deployment's agents (CognitiveLoop itself never grants or derives
    authority, only exercises what it was constructed with). Loop B's own model-produced completion
    is what reaches broker.dispatch(authority=...) here, not a directly-constructed Intent -- this
    is what makes delegation visibly agent-to-agent, landing in the full real chain: siphonophore-
    spawn, kernel-verified check-in, and Belnap reconciliation."""
    gate = Gate(ConsequencePolicy(mapping={"privileged": "uid_cgroup_checkin"}))
    checkin_backend = CheckedInSpawnHelperBackend(uid_min=TWO_LOOP_UID_MIN, uid_max=TWO_LOOP_UID_MAX, checkin_timeout=10.0)
    executor = Executor(gate, backends={
        "same_process": SameProcessBackend(allow_root=True),
        "uid_cgroup_checkin": checkin_backend,
    })
    broker = Broker(gate=gate, executor=executor)

    order = gate.issue_order("order-multiagent-001", "operator:ops-alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority_a = gate.grant_root_authority(order, principal_id="agent-a")

    loop_a_completion = json.dumps({
        "kind": "run_artifact", "consequence": "low", "artifact_code": "print('agent A did its own part')",
    })
    loop_a = CognitiveLoop(model=ScriptedModel([loop_a_completion]), broker=broker, principal_id="agent-a", authority=authority_a)
    effect_a = loop_a.step("do your own part of the task")
    assert effect_a.execution_class == "same_process"

    authority_b = gate.delegate(authority_a, to_principal_id="agent-a.sub-agent-b")
    loop_b_completion = json.dumps({
        "kind": "run_artifact", "consequence": "privileged",
        "payload": {"outdir": str(world_writable_outdir)}, "artifact_code": _AGENT_B_CODE,
    })
    loop_b = CognitiveLoop(model=ScriptedModel([loop_b_completion]), broker=broker, principal_id="agent-a.sub-agent-b", authority=authority_b)

    try:
        effect_b = loop_b.step("do the delegated subtask")
        assert effect_b.execution_class == "uid_cgroup_checkin"
        obs = effect_b.detail["observations"]
        assert obs["checkin"]["verified"] is True
        assert obs["reconciliation"]["b_did_this.txt"]["value"] == "corroborated"
    finally:
        checkin_backend.shutdown()


@requires_root_linux
def test_second_loop_cannot_exercise_outside_its_delegated_scope_via_a_real_completion():
    """The same scope-violation refusal already proven at the Broker level directly
    (test_harness_loop.py, portable) holds identically when B's out-of-scope request is produced
    by a real CognitiveLoop's own model completion, sharing a Gate with a second, independent loop
    (agent A), rather than constructed directly by test code."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    executor = Executor(gate, backends={"same_process": SameProcessBackend(allow_root=True)})
    broker = Broker(gate=gate, executor=executor)

    order = gate.issue_order("order-multiagent-002", "operator:ops-alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, principal_id="agent-a")
    loop_a = CognitiveLoop(model=ScriptedModel([json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})]),
                            broker=broker, principal_id="agent-a", authority=authority_a)
    loop_a.step("agent A's own ordinary turn")

    authority_b = gate.delegate(authority_a, to_principal_id="agent-a.sub-agent-b")
    out_of_scope_completion = json.dumps({"kind": "write_file", "consequence": "low", "artifact_code": "pass"})
    loop_b = CognitiveLoop(model=ScriptedModel([out_of_scope_completion]), broker=broker,
                            principal_id="agent-a.sub-agent-b", authority=authority_b)

    with pytest.raises(GateViolation):
        loop_b.step("try something agent B was never delegated")
