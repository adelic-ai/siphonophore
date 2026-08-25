"""Tests for Gate -- the core security properties lab/001, 002, 003, and 008 each independently
proved for one field at a time. Consolidated here as the real, permanent test suite for the
formalized package, not re-derived from scratch -- each test below traces to the lab experiment
that first proved the property."""
from __future__ import annotations

from dataclasses import replace

import pytest

from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate, digest_of
from siphonophore_core.policy import ConsequencePolicy, Decision


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy())


def test_submit_mints_a_decision_that_verifies(gate: Gate):
    intent = Intent(kind="write_file", principal_id="alice", intent_id="i-1", consequence="low")
    decision = gate.submit(intent)
    assert decision.permitted is True
    assert decision.execution_class == "same_process"
    assert gate.verify(decision) is True


def test_denied_intent_still_produces_a_verifiable_decision(gate: Gate):
    """Policy denial and Gate verification are separate concerns -- a denied Decision is still a
    genuine, verifiable Decision (permitted=False), not the absence of one. Executor is what
    refuses to act on it (execution.py), not Gate itself."""
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-1", consequence="low")
    decision = gate.submit(intent)
    assert decision.permitted is False
    assert gate.verify(decision) is True


def test_forged_decision_never_through_submit_fails_verification(gate: Gate):
    """lab/001's core proof: a hand-constructed Decision, never produced by Gate.submit(), fails
    verification. The token can't be guessed -- it's an HMAC keyed by a secret that never leaves
    the Gate instance."""
    forged = Decision(
        intent_id="i-1", principal_id="alice", kind="write_file",
        permitted=True, execution_class="same_process", artifact_digest="",
        token="0" * 64,
    )
    assert gate.verify(forged) is False


def test_forged_decision_using_a_different_gates_real_secret_still_fails(gate: Gate):
    """A sharper forgery attempt than a made-up token: mint a Decision with a DIFFERENT Gate
    instance (a different, real secret) and present it to this one. Proves verification is bound
    to a specific Gate's own secret, not just 'is this a well-formed HMAC.'"""
    other_gate = Gate(ConsequencePolicy())
    intent = Intent(kind="write_file", principal_id="alice", intent_id="i-1", consequence="low")
    decision_from_other_gate = other_gate.submit(intent)
    assert gate.verify(decision_from_other_gate) is False


def test_kind_tamper_fails_verification(gate: Gate):
    """lab/002's finding: kind is bound into the token. Relabeling a genuine Decision's kind,
    token left unchanged, must fail verification."""
    intent = Intent(kind="write_file", principal_id="alice", intent_id="i-1", consequence="low")
    decision = gate.submit(intent)
    tampered = replace(decision, kind="delegate")
    assert gate.verify(tampered) is False


def test_execution_class_downgrade_fails_verification(gate: Gate):
    """lab/003's finding, the same class of bug rediscovered independently: execution_class is
    bound into the token. Downgrading a genuine separate_process Decision to same_process, token
    left unchanged, must fail verification."""
    intent = Intent(kind="write_file", principal_id="alice", intent_id="i-1", consequence="high")
    decision = gate.submit(intent)
    assert decision.execution_class == "separate_process"
    downgraded = replace(decision, execution_class="same_process")
    assert gate.verify(downgraded) is False


def test_artifact_digest_tamper_fails_verification(gate: Gate):
    """lab/008's finding: artifact_digest is bound into the token. Swapping a genuine Decision's
    digest to a DIFFERENT real digest (not garbage), token left unchanged, must fail verification."""
    program_a = "print('A')"
    program_b = "print('B')"
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code=program_a)
    decision = gate.submit(intent)
    assert decision.artifact_digest == digest_of(program_a)
    tampered = replace(decision, artifact_digest=digest_of(program_b))
    assert gate.verify(tampered) is False


def test_permitted_flag_tamper_fails_verification(gate: Gate):
    """Not yet covered by any lab experiment individually, but the same binding discipline
    requires it: flipping a genuinely-denied Decision's permitted flag to True, token unchanged,
    must fail verification -- otherwise a denied Decision could be forged into an approved one
    without ever touching the token."""
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-1", consequence="low")
    decision = gate.submit(intent)
    assert decision.permitted is False
    tampered = replace(decision, permitted=True)
    assert gate.verify(tampered) is False


def test_intent_with_no_artifact_produces_no_fabricated_digest(gate: Gate):
    intent = Intent(kind="write_file", principal_id="alice", intent_id="i-1", consequence="low")
    decision = gate.submit(intent)
    assert decision.artifact_digest == ""
