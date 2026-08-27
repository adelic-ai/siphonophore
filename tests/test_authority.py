"""Tests for Order/Authority/Scope (authority.py) and Gate's authority-aware submit() (mediation.py)
-- the minimal, durable delegation model. Portable: no root/Linux needed, this is pure Gate logic.

See tests/test_harness_loop_linux.py for the one real end-to-end slice landing in UidCgroupBackend
-- this file covers the mechanism's own properties in isolation, including every negative case that
doesn't need a real OS boundary to demonstrate."""
from __future__ import annotations

from dataclasses import replace

import pytest

from siphonophore_core.execution import ArtifactMismatchError, Executor, SameProcessBackend
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))


def _executor(gate: Gate) -> Executor:
    # allow_root=True: these tests exercise Gate/authority logic, not the root-refusal feature.
    return Executor(gate, backends={"same_process": SameProcessBackend(allow_root=True)})


# ---- Order --------------------------------------------------------------------------------

def test_issue_order_mints_a_verifiable_order(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    assert gate.verify_order(order) is True


def test_forged_order_fails_verification(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    tampered = replace(order, issuer="operator:mallory")
    assert gate.verify_order(tampered) is False


# ---- Authority: root grant ------------------------------------------------------------------

def test_grant_root_authority_derives_from_a_verified_order(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority = gate.grant_root_authority(order, principal_id="agent-a")
    assert gate.verify_authority(authority) is True
    assert authority.order_id == order.order_id
    assert authority.parent_authority_id is None
    assert authority.scope.allowed_kinds == frozenset({"run_artifact"})
    assert authority.scope.remaining_delegation_depth == 2


def test_grant_root_authority_rejects_a_forged_order(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    forged = replace(order, granted_kinds=frozenset({"run_artifact", "write_file"}))  # token now stale
    with pytest.raises(GateViolation):
        gate.grant_root_authority(forged, principal_id="agent-a")


def test_grant_root_authority_cannot_request_kinds_the_order_does_not_grant(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    with pytest.raises(GateViolation):
        gate.grant_root_authority(order, principal_id="agent-a", allowed_kinds=frozenset({"run_artifact", "write_file"}))


# ---- Authority: delegation -------------------------------------------------------------------

def test_delegate_derives_a_narrower_authority_from_a_verified_parent(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact", "write_file"}), max_delegation_depth=3)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-b", allowed_kinds=frozenset({"run_artifact"}))
    assert gate.verify_authority(authority_b) is True
    assert authority_b.order_id == order.order_id
    assert authority_b.parent_authority_id == authority_a.authority_id
    assert authority_b.scope.allowed_kinds == frozenset({"run_artifact"})
    assert authority_b.scope.remaining_delegation_depth == authority_a.scope.remaining_delegation_depth - 1


def test_delegate_rejects_a_fabricated_parent_authority(gate: Gate):
    """Fabricated/missing lineage: an Authority never produced by grant_root_authority()/delegate(),
    with a garbage token, cannot be used as a delegation parent."""
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    real_authority = gate.grant_root_authority(order, "agent-a")
    fabricated = replace(real_authority, authority_id="not-a-real-id", token="0" * 64)
    with pytest.raises(GateViolation):
        gate.delegate(fabricated, "agent-b")


def test_delegate_cannot_expand_scope_beyond_the_parent(gate: Gate):
    """Attempted scope expansion at delegation-mint time."""
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority_a = gate.grant_root_authority(order, "agent-a", allowed_kinds=frozenset({"run_artifact"}))
    with pytest.raises(GateViolation):
        gate.delegate(authority_a, "agent-b", allowed_kinds=frozenset({"run_artifact", "write_file"}))


def test_delegate_refuses_when_depth_exhausted(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-b")
    assert authority_b.scope.remaining_delegation_depth == 0
    with pytest.raises(GateViolation):
        gate.delegate(authority_b, "agent-c")


def test_wrong_parent_root_spliced_between_two_real_independent_chains(gate: Gate):
    """Two independent, genuinely-minted chains; splice chain 1's authority to claim chain 2's
    (also real) authority as its parent, token unchanged -- must fail verification. Same mechanism
    lab/002's kind-relabel case proved, now on the lineage fields."""
    order_1 = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    order_2 = gate.issue_order("order-2", "operator:bob", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority_1a = gate.grant_root_authority(order_1, "agent-1a")
    authority_2a = gate.grant_root_authority(order_2, "agent-2a")

    spliced = replace(authority_1a, parent_authority_id=authority_2a.authority_id, order_id=order_2.order_id)
    assert gate.verify_authority(spliced) is False


# ---- Gate.submit() with an authority ----------------------------------------------------------

def test_submit_with_authority_binds_authority_and_order_into_the_decision(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority = gate.grant_root_authority(order, "agent-a")
    intent = Intent(kind="run_artifact", principal_id="agent-a", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = gate.submit(intent, authority=authority)
    assert decision.permitted is True
    assert decision.authority_id == authority.authority_id
    assert decision.order_id == order.order_id
    assert gate.verify(decision) is True


def test_submit_refuses_intent_outside_authoritys_scope(gate: Gate):
    """Effect requested is outside the delegated authority -- a real, signed refusal, not an
    exception (consistent with how an ordinary policy denial already works)."""
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority = gate.grant_root_authority(order, "agent-a")  # run_artifact only
    intent = Intent(kind="write_file", principal_id="agent-a", intent_id="i-1", consequence="low")
    decision = gate.submit(intent, authority=authority)
    assert decision.permitted is False
    assert gate.verify(decision) is True  # still a genuine, verifiable Decision -- just denied


def test_submit_refuses_principal_mismatch(gate: Gate):
    """Authority impersonation: a real, verified Authority minted for one principal cannot be used
    to submit an Intent claiming to be a different principal."""
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority = gate.grant_root_authority(order, "agent-a")
    intent = Intent(kind="run_artifact", principal_id="agent-mallory", intent_id="i-1", consequence="low", artifact_code="pass")
    with pytest.raises(GateViolation):
        gate.submit(intent, authority=authority)


def test_submit_refuses_forged_authority(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    real_authority = gate.grant_root_authority(order, "agent-a")
    forged = replace(real_authority, token="0" * 64)
    intent = Intent(kind="run_artifact", principal_id="agent-a", intent_id="i-1", consequence="low", artifact_code="pass")
    with pytest.raises(GateViolation):
        gate.submit(intent, authority=forged)


def test_submit_without_authority_is_unchanged_from_before_this_feature_existed(gate: Gate):
    """Backward compatibility, checked directly: the authority-less path behaves exactly as it did
    before Order/Authority existed -- authority_id/order_id both None, everything else identical."""
    intent = Intent(kind="run_artifact", principal_id="agent-a", intent_id="i-1", consequence="low", artifact_code="pass")
    decision = gate.submit(intent)
    assert decision.permitted is True
    assert decision.authority_id is None
    assert decision.order_id is None
    assert gate.verify(decision) is True


# ---- Composition with the existing, unrelated Executor mechanisms -----------------------------
# Authority/Scope govern WHETHER an intent may be attempted at all; artifact-digest binding and
# execution-class binding are separate, pre-existing, unrelated mechanisms that must keep working
# identically on an authority-grounded Decision -- these are not new checks, just demonstrating the
# existing ones compose with the new path.

def test_authority_grounded_decision_still_refuses_artifact_substitution(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority = gate.grant_root_authority(order, "agent-a")
    real_intent = Intent(kind="run_artifact", principal_id="agent-a", intent_id="i-1", consequence="low", artifact_code="print('A')")
    decision = gate.submit(real_intent, authority=authority)
    swapped_intent = replace(real_intent, artifact_code="print('B')")
    with pytest.raises(ArtifactMismatchError):
        _executor(gate).execute(decision, swapped_intent)


def test_authority_grounded_decision_still_refuses_execution_class_downgrade(gate: Gate):
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=2)
    authority = gate.grant_root_authority(order, "agent-a")
    intent = Intent(kind="run_artifact", principal_id="agent-a", intent_id="i-1", consequence="high", artifact_code="pass")
    decision = gate.submit(intent, authority=authority)
    assert decision.execution_class == "separate_process"
    downgraded = replace(decision, execution_class="same_process")
    assert gate.verify(downgraded) is False
