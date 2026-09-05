"""Tests for examples/repl.py's pure presentation helpers (Stage 4 of
docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md): rendering the structured DispatchResult/
OutcomeCategory surface siphonophore_harness.outcome and siphonophore_harness.composition already
provide, without duplicating their semantics.

No `test_repl.py` convention existed before this file (confirmed by the implementation plan); this
one is scoped narrowly to the rendering functions themselves -- real Broker/Gate/Executor objects
throughout, no mocking, matching every other harness test file's own convention. The REPL's
input()/print() loop itself is intentionally not driven here (no real Anthropic API call is
available in this environment); that loop contains no logic beyond calling these same functions
and printing their return values.

examples/ is not an installed package (deliberately -- it's a reference example, not
siphonophore_harness's own concern, per DESIGN.md section 6/8's boundary), so repl.py is loaded
directly from its file path rather than via a normal import.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from siphonophore_core.execution import ExecutionError, NoBackendRegisteredError
from siphonophore_core.execution import ArtifactMismatchError, DecisionVerificationError
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.composition import compose_profile, portable_profile
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.outcome import OutcomeCategory


def _load_repl():
    repl_path = Path(__file__).resolve().parent.parent / "examples" / "repl.py"
    spec = importlib.util.spec_from_file_location("reference_repl", repl_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repl = _load_repl()


# ---- category labels: every OutcomeCategory has a distinct rendered label -----------------------

def test_every_outcome_category_has_a_distinct_operator_label():
    assert set(repl._CATEGORY_LABELS) == set(OutcomeCategory)
    assert len(set(repl._CATEGORY_LABELS.values())) == len(OutcomeCategory)


# ---- startup banner: derived entirely from ExecutionProfile --------------------------------------

def test_startup_banner_discloses_profile_execution_classes_and_policy():
    profile = portable_profile()
    banner = repl.render_startup_banner(profile, model_id="claude-x", principal_id="human-operator", authority=None)
    assert "profile: portable" in banner
    assert "same_process" in banner and "separate_process" in banner
    assert "low->same_process" in banner
    assert "authority: none" in banner


def test_startup_banner_hides_authority_clutter_when_none_held():
    profile = portable_profile()
    banner = repl.render_startup_banner(profile, model_id="claude-x", principal_id="human-operator", authority=None)
    assert "authority_id" not in banner


def test_startup_banner_shows_safe_authority_metadata_when_held():
    profile = portable_profile()
    authority = repl._grant_root_authority(profile, "human-operator")
    banner = repl.render_startup_banner(profile, model_id="claude-x", principal_id="human-operator", authority=authority)
    assert f"authority_id={authority.authority_id}" in banner
    assert "run_artifact" in banner and "write_file" in banner
    assert "remaining_delegation_depth=1" in banner
    assert authority.token not in banner  # bearer capability material never rendered


# ---- authority-granting helper: uses only existing public Gate APIs -----------------------------

def test_grant_root_authority_helper_produces_a_real_verifiable_authority():
    profile = portable_profile()
    authority = repl._grant_root_authority(profile, "alice")
    assert authority.principal_id == "alice"
    assert authority.scope.allowed_kinds == frozenset(ConsequencePolicy.DEFAULT_ALLOWED_KINDS)
    assert authority.scope.remaining_delegation_depth == 1
    assert profile.gate.verify_authority(authority) is True  # a real, Gate-mintable Authority


# ---- successful turn rendering: intent_id/execution_class/outcome default-visible ----------------

def test_render_turn_result_same_process():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-same", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    rendered = repl.render_turn_result(result)
    assert "[executed]" in rendered
    assert "intent_id=i-same" in rendered
    assert "execution_class=same_process" in rendered


def test_render_turn_result_separate_process():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-sep", consequence="high", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    rendered = repl.render_turn_result(result)
    assert "[executed]" in rendered
    assert "execution_class=separate_process" in rendered


def test_render_turn_result_shows_authority_context_when_held():
    profile = portable_profile()
    authority = repl._grant_root_authority(profile, "alice")
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-auth", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent, authority=authority)
    rendered = repl.render_turn_result(result)
    assert f"authority_id={authority.authority_id}" in rendered
    assert f"order_id={authority.order_id}" in rendered


# ---- outcome/error rendering: classify_outcome() is the sole semantic source ---------------------

def test_render_outcome_error_denied_shows_decision_context():
    profile = portable_profile()
    intent = Intent(kind="not_a_real_kind", principal_id="alice", intent_id="i-deny", consequence="low")
    with pytest.raises(GateViolation) as exc_info:
        profile.broker.dispatch(intent)
    rendered = repl.render_outcome_error(exc_info.value)
    assert "[denied by policy]" in rendered
    assert "execution_class=" in rendered  # Broker attached a DecisionProjection: a Decision was minted


def test_render_outcome_error_input_rejected_has_no_decision_context():
    exc = IntentParseError("completion is not valid JSON")
    rendered = repl.render_outcome_error(exc)
    assert "[input rejected]" in rendered
    assert "execution_class=" not in rendered  # no Decision ever existed pre-Intent


def test_render_outcome_error_authority_rejected_has_no_decision_context():
    profile = portable_profile()
    order = profile.gate.issue_order("order-mismatch", "issuer", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority = profile.gate.grant_root_authority(order, principal_id="alice")
    intent = Intent(kind="run_artifact", principal_id="mallory", intent_id="i-mismatch", consequence="low", artifact_code="pass")
    with pytest.raises(GateViolation) as exc_info:
        profile.broker.dispatch(intent, authority=authority)
    rendered = repl.render_outcome_error(exc_info.value)
    assert "[authority rejected]" in rendered
    assert "execution_class=" not in rendered  # raised inside Gate.submit(), before any Decision existed


def test_render_outcome_error_backend_unavailable():
    profile = compose_profile("no-backends-test", backends={}, policy_mapping=ConsequencePolicy.DEFAULT_MAPPING)
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-nobackend", consequence="low", artifact_code="pass")
    with pytest.raises(NoBackendRegisteredError) as exc_info:
        profile.broker.dispatch(intent)
    rendered = repl.render_outcome_error(exc_info.value)
    assert "[backend unavailable]" in rendered


def test_render_outcome_error_execution_failed():
    profile = portable_profile()
    intent = Intent(
        kind="run_artifact", principal_id="alice", intent_id="i-fail", consequence="high",
        artifact_code="import sys; sys.exit(1)",
    )
    with pytest.raises(ExecutionError) as exc_info:
        profile.broker.dispatch(intent)
    rendered = repl.render_outcome_error(exc_info.value)
    assert "[execution failed]" in rendered


@pytest.mark.parametrize(
    "exc, label",
    [
        pytest.param(DecisionVerificationError("forged"), "[integrity verification failed]", id="decision-verification"),
        pytest.param(ArtifactMismatchError("swapped"), "[integrity verification failed]", id="artifact-mismatch"),
    ],
)
def test_render_outcome_error_integrity_rejected(exc, label):
    # Unreachable through Broker.dispatch()'s own path (docs/REFERENCE_HARNESS_TARGET_DESIGN.md
    # section 9) -- constructed directly, matching tests/test_harness_outcome.py's own convention.
    assert label in repl.render_outcome_error(exc)


def test_render_outcome_error_unknown_internal_state_is_not_mislabeled():
    """A plain RuntimeError -- not ExecutionError -- is intentionally outside classify_outcome()'s
    closed category set. It must render as an explicit unknown/internal state, never silently as
    execution_failed (which would fabricate semantic certainty classify_outcome() doesn't have)."""
    exc = RuntimeError("boom")
    rendered = repl.render_outcome_error(exc)
    assert "[unknown/internal error]" in rendered
    assert "RuntimeError" in rendered and "boom" in rendered
    for label in repl._CATEGORY_LABELS.values():
        assert f"[{label}]" not in rendered
