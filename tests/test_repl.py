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
import io
from pathlib import Path

import pytest

from siphonophore_core.execution import ExecutionError, NoBackendRegisteredError
from siphonophore_core.execution import ArtifactMismatchError, DecisionVerificationError
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.composition import compose_profile, planning_profile, portable_profile
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.work_order import WorkOrder
from siphonophore_harness.model_anthropic import ModelResponseDiagnostics
from siphonophore_harness.outcome import OperationOutcome, OutcomeCategory, TurnResult


def _load_repl():
    repl_path = Path(__file__).resolve().parent.parent / "examples" / "repl.py"
    spec = importlib.util.spec_from_file_location("reference_repl", repl_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repl = _load_repl()


def _turn_result_from_dispatch(result, *, message=None, exhausted=False):
    """Wraps a real, single DispatchResult (from a direct Broker.dispatch() call, the pattern
    every test in this file already uses) into the TurnResult shape render_turn() now consumes
    (docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md) -- a one-operation turn is the smallest,
    still-real instance of the general multi-cycle shape, not a special case of it."""
    outcome = OperationOutcome(
        intent_id=result.intent_id,
        category=OutcomeCategory.EXECUTED,
        execution_class=result.execution_class,
        authority_id=result.decision.authority_id,
        order_id=result.decision.order_id,
        detail=result.detail,
        reason=None,
    )
    return TurnResult(message=message, operations=(outcome,), exhausted=exhausted)


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


# ---- Stage 4A: empty detail is noise, non-empty detail is meaningful -----------------------------

def test_render_turn_result_omits_empty_detail():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-empty-detail", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    assert result.detail == {}  # confirms this is genuinely the empty-detail case, not incidentally
    rendered = repl.render_turn_result(result)
    assert "detail=" not in rendered


def test_render_turn_result_shows_non_empty_detail():
    # separate_process's own backend (execution.py) genuinely populates detail with subprocess
    # stdout -- using the real path here rather than fabricating an Effect.
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-detail", consequence="high", artifact_code="print('hi')")
    result = profile.broker.dispatch(intent)
    assert result.detail  # confirms this really is the non-empty-detail case
    rendered = repl.render_turn_result(result)
    assert "detail=" in rendered
    assert "stdout" in rendered


# ---- Stage 4A: normal trace still carries outcome/execution_class/intent_id ----------------------

def test_render_turn_result_always_includes_outcome_execution_class_and_intent_id():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-trace-fields", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    rendered = repl.render_turn_result(result)
    assert "[executed]" in rendered
    assert "execution_class=same_process" in rendered
    assert "intent_id=i-trace-fields" in rendered


# ---- Stage 4A: Claude's conversational reply renders before the compact trace ---------------------

def test_render_turn_places_message_before_trace():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-order", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message="The capital of Japan is Tokyo.")
    rendered = repl.render_turn(turn_result, verbose=False)
    assert rendered.index("The capital of Japan is Tokyo.") < rendered.index("[executed]")


def test_render_turn_shows_placeholder_when_no_message():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-no-message", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message=None)
    rendered = repl.render_turn(turn_result, verbose=False)
    assert rendered.index("[no message this turn]") < rendered.index("[executed]")


def test_render_turn_retains_trace_fields_and_omits_verbose_by_default():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-verbose-off", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message="hi")
    rendered = repl.render_turn(turn_result, verbose=False)
    assert "[raw completion]" not in rendered
    assert "execution_class=same_process" in rendered
    assert "intent_id=i-verbose-off" in rendered


def test_render_turn_includes_raw_completion_when_verbose():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-verbose-on", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message="hi")
    rendered = repl.render_turn(turn_result, verbose=True, raw_completion="raw model text")
    assert "[raw completion]" in rendered
    assert "raw model text" in rendered
    assert rendered.index("[executed]") < rendered.index("[raw completion]")  # trace, then verbose detail, last


# ---- turn contract: message-only rendering has no execution trace footer at all -------------------

def test_render_turn_of_a_message_only_result_shows_the_message_and_no_trace_footer():
    turn_result = TurnResult(message="The capital of Japan is Tokyo.", operations=(), exhausted=False)
    rendered = repl.render_turn(turn_result, verbose=False)
    assert rendered.strip() == "The capital of Japan is Tokyo."
    for label in repl._CATEGORY_LABELS.values():
        assert f"[{label}]" not in rendered
    assert "intent_id" not in rendered
    assert "execution_class" not in rendered


def test_render_turn_of_a_message_only_result_with_no_message_shows_only_the_placeholder():
    turn_result = TurnResult(message=None, operations=(), exhausted=False)
    rendered = repl.render_turn(turn_result, verbose=False)
    assert rendered.strip() == "[no message this turn]"


def test_render_turn_of_a_message_only_result_still_shows_raw_completion_when_verbose():
    turn_result = TurnResult(message="hello", operations=(), exhausted=False)
    rendered = repl.render_turn(turn_result, verbose=True, raw_completion='{"message": "hello"}')
    assert "[raw completion]" in rendered
    assert '{"message": "hello"}' in rendered
    for label in repl._CATEGORY_LABELS.values():
        assert f"[{label}]" not in rendered


# ---- continuation: multiple operations render as multiple compact trace lines, in order -----------

def test_render_turn_of_multiple_operations_shows_one_trace_line_each_in_order():
    outcome_a = OperationOutcome(
        intent_id="i-a", category=OutcomeCategory.EXECUTED, execution_class="same_process",
        authority_id=None, order_id=None, detail={}, reason=None,
    )
    outcome_b = OperationOutcome(
        intent_id="i-b", category=OutcomeCategory.DENIED, execution_class="same_process",
        authority_id=None, order_id=None, detail={}, reason="intent was not permitted by policy",
    )
    turn_result = TurnResult(message="here's what happened", operations=(outcome_a, outcome_b), exhausted=False)

    rendered = repl.render_turn(turn_result, verbose=False)

    assert rendered.index("here's what happened") < rendered.index("i-a") < rendered.index("i-b")
    assert "[executed]" in rendered
    assert "[denied by policy]" in rendered
    assert "reason=intent was not permitted by policy" in rendered


def test_render_turn_of_an_exhausted_turn_shows_a_distinct_safety_net_line():
    outcome = OperationOutcome(
        intent_id="i-a", category=OutcomeCategory.EXECUTED, execution_class="same_process",
        authority_id=None, order_id=None, detail={}, reason=None,
    )
    turn_result = TurnResult(message="I tried to do more but hit a limit", operations=(outcome,), exhausted=True)

    rendered = repl.render_turn(turn_result, verbose=False)

    assert "[operation safety-net reached]" in rendered
    assert "1 operations attempted this turn" in rendered
    for label in repl._CATEGORY_LABELS.values():
        assert "[operation safety-net reached]" != f"[{label}]"  # never confusable with a real OutcomeCategory label


def test_render_turn_of_a_loop_detected_turn_shows_a_distinct_line():
    outcome = OperationOutcome(
        intent_id="i-a", category=OutcomeCategory.EXECUTED, execution_class="same_process",
        authority_id=None, order_id=None, detail={}, reason=None,
    )
    turn_result = TurnResult(
        message="stop repeating yourself", operations=(outcome,), exhausted=False, loop_detected=True,
    )

    rendered = repl.render_turn(turn_result, verbose=False)

    assert "[repeated operation detected]" in rendered
    assert "[operation safety-net reached]" not in rendered


def test_render_operation_outcome_omits_execution_class_for_authority_rejected():
    outcome = OperationOutcome(
        intent_id="i-a", category=OutcomeCategory.AUTHORITY_REJECTED, execution_class=None,
        authority_id=None, order_id=None, detail={}, reason="authority failed Gate verification",
    )
    rendered = repl.render_operation_outcome(outcome)
    assert "execution_class=" not in rendered
    assert "[authority rejected]" in rendered
    assert "reason=authority failed Gate verification" in rendered


# ---- Stage 4A: optional readline/libedit line-editing degrades gracefully -------------------------

def test_repl_module_is_importable_without_readline(monkeypatch):
    """Confirms only that examples/repl.py doesn't hard-depend on readline -- some minimal/embedded
    Python builds lack it. This does NOT and cannot verify actual macOS terminal history/arrow-key
    behavior; that remains a real-Mac-terminal empirical check, not something a headless test can
    honestly establish."""
    import builtins

    real_import = builtins.__import__

    def _no_readline(name, *args, **kwargs):
        if name == "readline":
            raise ImportError("simulated: readline unavailable in this build")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_readline)
    module = _load_repl()
    assert module.readline is None


def test_repl_adds_no_custom_terminal_input_escape_parsing():
    """Stage 4A guarantee, narrowed for Stage 4B: no custom parsing of arrow-key/cursor-movement
    *input* escape sequences -- readline (or its absence) remains the sole input-editing mechanism.
    Stage 4B's clear_screen() writes a fixed ANSI clear/home *output* sequence (\\x1b[2J\\x1b[H); that's
    presentation, not input parsing, so it's exempted rather than forbidden outright."""
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    for arrow in ("\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"):
        assert arrow not in source
    assert "\\033[" not in source


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


def test_render_outcome_error_with_no_prior_operations_shows_no_effects_notice():
    exc = IntentParseError("completion is not valid JSON")
    rendered = repl.render_outcome_error(exc, operations=())
    assert "already dispatched" not in rendered


def test_render_outcome_error_truthfully_shows_operations_that_already_happened():
    """V1.1: a real trial's terminal presentation read as if the original input had simply been
    rejected before anything happened, even though several real operations had already been
    dispatched and mediated earlier in the same failed turn. render_outcome_error(operations=...)
    (CognitiveLoop.last_operations, loop.py) must surface that truth ahead of the error itself."""
    prior_op = OperationOutcome(
        intent_id="i-1", category=OutcomeCategory.EXECUTED, execution_class="same_process",
        authority_id=None, order_id=None, detail={"result": 42}, reason=None,
    )
    exc = IntentParseError("completion is not valid JSON")
    rendered = repl.render_outcome_error(exc, operations=(prior_op,))
    assert "1 real operation(s) were already dispatched and mediated this turn" in rendered
    assert "[executed]" in rendered
    assert "i-1" in rendered
    assert "[input rejected]" in rendered
    # the truthful notice comes first, ahead of the error line, matching operations' own dispatch order
    assert rendered.index("already dispatched") < rendered.index("[input rejected]")


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


# ---- Stage 4B: fixed ASCII banner ------------------------------------------------------------

def test_render_logo_contains_siphonophore():
    assert "SIPHONOPHORE" in repl.render_logo()


def test_render_logo_contains_tagline():
    assert "mediated agent execution" in repl.render_logo()


# ---- Stage 4B: interactive clear-screen decision, presentation-only and portable ---------------

class _FakeStream:
    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def test_should_clear_screen_true_for_interactive_tty():
    assert repl.should_clear_screen(stream=_FakeStream(True), term="xterm-256color", no_clear=False) is True


def test_should_clear_screen_false_when_no_clear_passed():
    assert repl.should_clear_screen(stream=_FakeStream(True), term="xterm-256color", no_clear=True) is False


def test_should_clear_screen_false_for_non_tty_stream():
    assert repl.should_clear_screen(stream=_FakeStream(False), term="xterm-256color", no_clear=False) is False


def test_should_clear_screen_false_for_term_dumb():
    assert repl.should_clear_screen(stream=_FakeStream(True), term="dumb", no_clear=False) is False


def test_should_clear_screen_false_when_stream_has_no_isatty():
    class _NoIsatty:
        pass

    assert repl.should_clear_screen(stream=_NoIsatty(), term="xterm-256color", no_clear=False) is False


def test_clear_screen_writes_ansi_clear_and_home():
    buf = io.StringIO()
    repl.clear_screen(buf)
    assert buf.getvalue() == "\x1b[2J\x1b[H"


def test_no_clear_flag_registered_with_expected_help_text():
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    assert '"--no-clear"' in source
    assert "do not clear the terminal on interactive startup" in source


# ---- Stage 4B: startup ordering -- clear, then banner, then capability/session info -------------

def test_main_clears_before_printing_logo_before_capability_banner():
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    main_body = source[source.index("def main("):]
    clear_idx = main_body.index("clear_screen(")
    logo_idx = main_body.index("render_logo()")
    capability_idx = main_body.index("render_startup_banner(")
    assert clear_idx < logo_idx < capability_idx


# ---- model-boundary observability: diagnostics summary rendering ---------------------------------

def test_render_model_diagnostics_shows_block_types_and_counts():
    diagnostics = ModelResponseDiagnostics(
        block_types=("thinking", "text"), text_block_count=1, total_block_count=2, retained_text_length=247,
    )
    rendered = repl.render_model_diagnostics(diagnostics)
    assert "['thinking', 'text']" in rendered
    assert "text_block_count=1" in rendered
    assert "retained_text_length=247" in rendered


def test_render_model_diagnostics_never_exposes_api_key():
    diagnostics = ModelResponseDiagnostics(
        block_types=("text",), text_block_count=1, total_block_count=1, retained_text_length=5,
    )
    rendered = repl.render_model_diagnostics(diagnostics)
    assert "sk-" not in rendered


# ---- Stage: verbose successful-turn output carries diagnostics alongside raw completion ----------

def test_render_turn_verbose_includes_diagnostics_when_provided():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-diag", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message="hi")
    diagnostics = ModelResponseDiagnostics(
        block_types=("text",), text_block_count=1, total_block_count=1, retained_text_length=9,
    )
    rendered = repl.render_turn(turn_result, verbose=True, raw_completion="raw text", diagnostics=diagnostics)
    assert "[raw completion]" in rendered
    assert "text_block_count=1" in rendered
    assert rendered.index("[executed]") < rendered.index("text_block_count=1") < rendered.index("[raw completion]")


def test_render_turn_verbose_omits_diagnostics_line_when_none_available():
    """ScriptedModel-backed turns (no diagnostics available) must not gain a fabricated diagnostics
    line -- verbose output degrades to exactly what it already showed before this stage."""
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-nodiag", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message="hi")
    rendered = repl.render_turn(turn_result, verbose=True, raw_completion="raw text", diagnostics=None)
    assert "[raw completion]" in rendered
    assert "block_types" not in rendered
    assert "text_block_count" not in rendered


def test_render_turn_normal_mode_never_shows_diagnostics_even_if_provided():
    profile = portable_profile()
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-normal-diag", consequence="low", artifact_code="pass")
    result = profile.broker.dispatch(intent)
    turn_result = _turn_result_from_dispatch(result, message="hi")
    diagnostics = ModelResponseDiagnostics(
        block_types=("text",), text_block_count=1, total_block_count=1, retained_text_length=9,
    )
    rendered = repl.render_turn(turn_result, verbose=False, raw_completion="raw text", diagnostics=diagnostics)
    assert "block_types" not in rendered
    assert "[raw completion]" not in rendered


# ---- Stage: failed-parse visibility -- verbose mode exposes diagnostics + raw retained completion -

def test_render_outcome_error_normal_mode_stays_concise_even_with_diagnostics_available():
    exc = IntentParseError("completion is not valid JSON: Expecting value: line 1 column 1 (char 0)")
    diagnostics = ModelResponseDiagnostics(
        block_types=("thinking",), text_block_count=0, total_block_count=1, retained_text_length=0,
    )
    rendered = repl.render_outcome_error(exc, verbose=False, raw_completion="", diagnostics=diagnostics)
    assert "[input rejected]" in rendered
    assert "block_types" not in rendered
    assert "[raw completion]" not in rendered


def test_render_outcome_error_verbose_shows_diagnostics_and_raw_completion():
    exc = IntentParseError("completion is not valid JSON: Expecting value: line 1 column 1 (char 0)")
    diagnostics = ModelResponseDiagnostics(
        block_types=("thinking",), text_block_count=0, total_block_count=1, retained_text_length=0,
    )
    rendered = repl.render_outcome_error(exc, verbose=True, raw_completion="", diagnostics=diagnostics)
    assert "[input rejected]" in rendered
    assert "text_block_count=0" in rendered
    assert "retained_text_length=0" in rendered
    assert "[raw completion]" in rendered


def test_render_outcome_error_verbose_shows_raw_completion_even_without_diagnostics():
    """A parse failure driven by ScriptedModel (no diagnostics available) must still surface the
    raw retained completion under --verbose -- diagnostics are additive, not a prerequisite."""
    exc = IntentParseError("completion is not valid JSON: Expecting value: line 1 column 1 (char 0)")
    rendered = repl.render_outcome_error(exc, verbose=True, raw_completion="not json at all", diagnostics=None)
    assert "[input rejected]" in rendered
    assert "not json at all" in rendered
    assert "block_types" not in rendered


def test_render_outcome_error_default_verbose_false_is_unchanged():
    """Existing callers (none of the other render_outcome_error tests in this file pass verbose=)
    must see byte-for-byte the same concise rendering as before this stage."""
    exc = IntentParseError("completion is not valid JSON")
    assert repl.render_outcome_error(exc) == repl.render_outcome_error(exc, verbose=False)


# ---- Stage: startup text no longer claims every message reaches parse_intent -> Gate -> Executor -

def test_startup_text_does_not_claim_every_message_reaches_parse_intent_and_gate():
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    assert "parse_intent -> Gate -> Executor" not in source


def test_startup_text_distinguishes_model_turns_from_mediated_operations():
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    assert "Every message becomes a model turn" in source
    assert "Gate -> Executor" in source


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


# ---- V1: capability-truthful startup banner shows allowed_kinds and session log info -------------

def test_startup_banner_shows_allowed_kinds_for_planning_profile():
    profile = planning_profile(root=".")
    banner = repl.render_startup_banner(profile, model_id="claude-x", principal_id="human-operator", authority=None)
    assert "allowed operation kinds: list_directory, read_file, search_repository" in banner


def test_startup_banner_shows_session_log_path_when_enabled():
    profile = portable_profile()
    banner = repl.render_startup_banner(
        profile, model_id="claude-x", principal_id="human-operator", authority=None,
        session_id="abc123", session_log_path="siphonophore-sessions/abc123.jsonl",
    )
    assert "session_id=abc123" in banner
    assert "siphonophore-sessions/abc123.jsonl" in banner


def test_startup_banner_shows_disabled_when_no_session_log_path():
    profile = portable_profile()
    banner = repl.render_startup_banner(
        profile, model_id="claude-x", principal_id="human-operator", authority=None,
        session_id="abc123", session_log_path=None,
    )
    assert "DISABLED" in banner


# ---- V1: CLI surface for profile/root/session-log/operation-bound selection -----------------------

def test_cli_has_profile_flag_defaulting_to_planning():
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    assert '"--profile"' in source
    assert 'default="planning"' in source


def test_cli_has_root_session_log_and_operation_bound_flags():
    source = (Path(__file__).resolve().parent.parent / "examples" / "repl.py").read_text()
    flags = (
        "--root", "--session-log", "--no-session-log",
        "--max-operations-per-turn-safety-net", "--repeated-operation-limit",
    )
    for flag in flags:
        assert f'"{flag}"' in source


# ---- V1: WorkOrder rendering ------------------------------------------------------------------

def _work_order(status="final", **overrides):
    fields = dict(
        work_order_id="wo-1", status=status, objective="add a feature",
        prompt="implement X exactly as discussed",
    )
    fields.update(overrides)
    return WorkOrder(**fields)


def test_render_work_order_shows_status_objective_and_prompt():
    rendered = repl.render_work_order(_work_order())
    assert "work_order:final" in rendered
    assert "add a feature" in rendered
    assert "implement X exactly as discussed" in rendered


def test_render_work_order_states_it_grants_no_authority():
    rendered = repl.render_work_order(_work_order())
    assert "grants no authority" in rendered


def test_render_work_order_shows_nonempty_optional_fields_only():
    wo = _work_order(requirements=("req1",), constraints=())
    rendered = repl.render_work_order(wo)
    assert "requirements: ['req1']" in rendered
    assert "constraints:" not in rendered  # empty tuple -- omitted, not shown as []


def test_render_turn_shows_work_order_after_message():
    from siphonophore_harness.outcome import TurnResult

    result = TurnResult(message="Here's the compiled plan.", operations=(), exhausted=False, work_order=_work_order())
    rendered = repl.render_turn(result, verbose=False)
    assert rendered.index("Here's the compiled plan.") < rendered.index("work_order:final")


def test_render_turn_of_message_only_result_has_no_work_order_section():
    from siphonophore_harness.outcome import TurnResult

    result = TurnResult(message="hi", operations=(), exhausted=False, work_order=None)
    rendered = repl.render_turn(result, verbose=False)
    assert "work_order" not in rendered
