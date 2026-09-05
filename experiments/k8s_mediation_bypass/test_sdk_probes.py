"""Cluster-free SDK checks -- criterion 6, bypass cases C2/C3, falsification F-08.

These assert a real property of Siphonophore, using real Siphonophore types, with no cluster and no
credential. They also pin the distinction the pre-registration insists on: what they demonstrate is
INTERNAL MEDIATION ENFORCEMENT, never bypass resistance.
"""
from __future__ import annotations

from siphonophore_core.execution import ArtifactMismatchError
from siphonophore_core.mediation import GateViolation

from sipho_bypass.evidence import (
    MECH_ARTIFACT_DIGEST_REJECTED, MECH_GATE_REJECTED_DECISION, Verdict,
)
from sipho_bypass.requester import probe_sdk


def test_decision_minted_by_a_different_gate_is_refused():
    result = probe_sdk.probe_forged_decision_wrong_gate()
    assert result.is_gate_violation is True
    assert result.is_artifact_mismatch is False
    assert result.backend_invocations == 0
    # Source truth recorded, not just asserted.
    assert result.raised_type == GateViolation.__name__


def test_fabricated_decision_with_a_made_up_token_is_refused():
    result = probe_sdk.probe_fabricated_decision()
    assert result.is_gate_violation is True
    assert result.backend_invocations == 0


def test_artifact_substitution_is_refused_before_the_backend_runs():
    result = probe_sdk.probe_artifact_substitution()
    assert result.is_artifact_mismatch is True
    assert result.raised_type == ArtifactMismatchError.__name__
    assert result.backend_invocations == 0          # ordering asserted, not inferred


def test_artifact_mismatch_subclasses_gate_violation_so_the_classifier_must_distinguish_them():
    """`ArtifactMismatchError(GateViolation)` (execution.py:53). A classifier that only checked
    `isinstance(exc, GateViolation)` would report a digest rejection as a token rejection."""
    assert issubclass(ArtifactMismatchError, GateViolation)
    forged = probe_sdk.to_case(probe_sdk.probe_forged_decision_wrong_gate(), want_artifact_mismatch=False)
    swapped = probe_sdk.to_case(probe_sdk.probe_artifact_substitution(), want_artifact_mismatch=True)
    assert forged.observed_mechanism == MECH_GATE_REJECTED_DECISION
    assert swapped.observed_mechanism == MECH_ARTIFACT_DIGEST_REJECTED


def test_all_sdk_cases_pass_and_are_labelled_as_sdk_evidence_only():
    cases = probe_sdk.run_all()
    assert len(cases) == 3
    for case in cases:
        assert case.verdict is Verdict.PASS
        assert case.evidence_categories == ("S",)
        assert "ays nothing about bypass resistance" in case.notes


def test_the_probe_backend_can_never_produce_a_real_effect():
    backend = probe_sdk.NeverRunsBackend()
    assert backend.invocations == 0
    assert not hasattr(backend, "_kubectl")
