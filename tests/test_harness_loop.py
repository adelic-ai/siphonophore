"""Tests for CognitiveLoop.step(): the full prompt -> completion -> parse turn -> (zero or more
bounded, independently mediated operation/result cycles) -> final TurnResult
(docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md), including the case that matters most for
DESIGN.md section 7's proof -- a hostile completion that tries to describe more authority than it
should get, or to smuggle fields outside Intent's schema, is refused by parse_intent/Broker exactly
the same way any other bad input is, because the loop has no other way to produce an effect -- and
the case that matters most for the reference-harness turn contract -- a completion naming no
operation at all must never reach Broker.dispatch(), Gate, or Executor, and must never be
misclassified as if it had."""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import (
    DecisionVerificationError,
    Executor,
    SameProcessBackend,
    SeparateProcessBackend,
)
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.model import ScriptedModel
from siphonophore_harness.outcome import OutcomeCategory, TurnResult


def _make_loop(completions: list[str], **kwargs) -> CognitiveLoop:
    gate = Gate(ConsequencePolicy())
    # allow_root=True: this file tests CognitiveLoop's own dispatch logic, not the root-refusal
    # feature (see test_execution_root_refusal.py) -- the full suite also runs as real root on
    # colima, and these portable tests should exercise the same logic there too.
    backends = {
        "same_process": SameProcessBackend(allow_root=True),
        "separate_process": SeparateProcessBackend(allow_root=True),
    }
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    return CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice", **kwargs)


class _CountingBroker:
    """Test-only spy standing in for Broker: proves a code path never calls dispatch() at all,
    rather than merely asserting on its return value. Mirrors the counting-backend convention
    already used by tests/test_harness_composition.py."""

    def __init__(self) -> None:
        self.call_count = 0

    def dispatch(self, intent, authority=None):
        self.call_count += 1
        raise AssertionError("Broker.dispatch() must never be called for a message-only turn")


class _CountingRealBroker:
    """Wraps a real Broker, counting dispatch() calls while still performing them for real --
    proves the hard per-turn operation bound refuses the N+1th operation BEFORE Broker.dispatch()
    is ever reached, not merely that its outcome is discarded afterward."""

    def __init__(self, real_broker: Broker) -> None:
        self._real_broker = real_broker
        self.call_count = 0

    def dispatch(self, intent, authority=None):
        self.call_count += 1
        return self._real_broker.dispatch(intent, authority=authority)


class _IntegrityRejectingBroker:
    """Test-only stand-in proving CognitiveLoop fails closed on INTEGRITY_REJECTED. This condition
    is practically unreachable through a real Broker.dispatch() call (Broker always executes with
    the same Intent it minted the Decision from), so it is constructed directly here, matching
    tests/test_harness_outcome.py's own convention for this same category."""

    def dispatch(self, intent, authority=None):
        raise DecisionVerificationError("forged decision, simulated for this test")


def _make_loop_with_broker(completions: list[str], broker, **kwargs) -> CognitiveLoop:
    return CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice", **kwargs)


def _op(kind="run_artifact", consequence="low", artifact_code="pass", message=None):
    body = {"operation": {"kind": kind, "consequence": consequence, "artifact_code": artifact_code}}
    if message is not None:
        body["message"] = message
    return json.dumps(body)


def _msg(message=None):
    return json.dumps({"message": message} if message is not None else {})


# ---- one operation, then a final response (acceptance scenario: single successful operation) ----

def test_step_dispatches_one_operation_and_returns_the_final_turn_result():
    loop = _make_loop([_op(), _msg("all done")])
    result = loop.step("please run something")

    assert isinstance(result, TurnResult)
    assert result.message == "all done"
    assert result.exhausted is False
    assert len(result.operations) == 1
    assert result.operations[0].category == OutcomeCategory.EXECUTED
    assert result.operations[0].execution_class == "same_process"


def test_step_feeds_the_operation_result_back_into_history_before_the_next_model_call():
    loop = _make_loop([_op(), _msg("all done")])
    loop.step("please run something")

    roles = [entry["role"] for entry in loop.history]
    assert roles == ["user", "assistant", "effect", "assistant", "effect"]
    assert "same_process" in loop.history[2]["content"]  # the first cycle's operation outcome
    assert loop.history[-1]["content"] == "no operation requested this turn"  # the final cycle


def test_second_step_sees_first_steps_history():
    loop = _make_loop([_op(), _msg("first done"), _op(), _msg("second done")])
    loop.step("first")
    history_before_second_call = list(loop.history)
    loop.step("second")
    # the model's later complete() calls were handed the accumulated history from the first turn
    assert loop.history[: len(history_before_second_call)] == history_before_second_call


# ---- protocol violations (INPUT_REJECTED) still fail closed at any cycle, unchanged -------------

def test_hostile_completion_naming_unknown_fields_is_refused_before_any_dispatch():
    """A completion that tries to smuggle a pre-authorized-looking field (e.g. "token") past the
    Gate never gets the chance -- parse_turn()/parse_intent() rejects it outright, and nothing
    resembling an Effect is ever produced."""
    hostile = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "token": "trust-me-bro"}})
    loop = _make_loop([hostile])
    with pytest.raises(IntentParseError):
        loop.step("do something")
    # transactional history (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): nothing committed at all
    # for a turn that fails before any real mediation attempt -- not even the user's own message,
    # so it cannot dangle and contaminate the next, unrelated turn.
    assert loop.history == []


def test_operation_missing_artifact_code_fails_before_broker_dispatch_and_is_not_message_only():
    """An operation present but missing required execution material must fail closed -- it must
    not reach Broker.dispatch(), and it must not be silently downgraded to a message-only success
    just because a conversational message happened to be present alongside it."""
    broker = _CountingBroker()
    hollow = json.dumps({"message": "I'll do that.", "operation": {"kind": "run_artifact", "consequence": "low"}})
    loop = _make_loop_with_broker([hollow], broker)

    with pytest.raises(IntentParseError):
        loop.step("do the thing")

    assert broker.call_count == 0


def test_input_rejected_on_a_continuation_cycle_still_fails_closed():
    """A parse failure at cycle 2 (after a real operation already dispatched at cycle 1) must
    propagate exactly as a cycle-1 parse failure always has -- INPUT_REJECTED is not recoverable in
    this stage (docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md's Success/Denial/Failure Semantics)."""
    loop = _make_loop([_op(), "not valid json at all"])
    with pytest.raises(IntentParseError):
        loop.step("do something")
    # the first cycle's operation was still fully, honestly recorded; the second (failing)
    # cycle's completion is not appended, exactly as a cycle-1 parse failure already never was
    # (the pre-existing alternation wrinkle this design carries forward unchanged, per
    # docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md's History Semantics)
    assert [entry["role"] for entry in loop.history] == ["user", "assistant", "effect"]


# ---- denial is recoverable: no effect occurs, but the model may explain itself ------------------

def test_denied_operation_is_not_silently_run_and_lets_the_model_explain():
    # "write_file" is code-bearing (artifact_code is required/allowed for it) but this Gate's own
    # policy only allows "run_artifact" -- triggering an ordinary policy DENY, not a parser-level
    # rejection.
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact",)))
    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    denied_then_explain = [
        _op(kind="write_file", message="I'll try this forbidden thing"),
        _msg("That action was not permitted, so I did not run it."),
    ]
    loop = _make_loop_with_broker(denied_then_explain, broker)

    result = loop.step("do something forbidden")

    assert result.message == "That action was not permitted, so I did not run it."
    assert len(result.operations) == 1
    assert result.operations[0].category == OutcomeCategory.DENIED
    assert result.operations[0].detail == {}  # no effect occurred


def test_loop_holding_a_delegated_authority_is_refused_outside_its_scope_but_can_recover():
    """The scope-violation refusal already proven at the Broker level (test_harness_broker.py)
    holds identically when the intent is produced by a real CognitiveLoop's own completion, not
    constructed directly by test code -- and, per the continuation contract, is now a recoverable
    DENIED outcome rather than a raised exception, since Gate.submit() folds an out-of-scope kind
    into `permitted=False` alongside the ordinary policy result (mediation.py), not a pre-Decision
    refusal."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    order = gate.issue_order("order-2", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    out_of_scope_then_explain = [
        _op(kind="write_file"),
        _msg("I couldn't do that -- it's outside what I was delegated."),
    ]
    loop_b = CognitiveLoop(
        model=ScriptedModel(out_of_scope_then_explain), broker=broker,
        principal_id="agent-a.sub-agent-b", authority=authority_b,
    )

    result = loop_b.step("try something outside what was delegated")

    assert result.operations[0].category == OutcomeCategory.DENIED
    assert result.message == "I couldn't do that -- it's outside what I was delegated."


# ---- backend_unavailable and execution_failed remain distinct categories ------------------------

def test_backend_unavailable_and_execution_failed_remain_distinct_categories():
    gate = Gate(ConsequencePolicy())
    no_backend_broker = Broker(gate=gate, executor=Executor(gate, backends={}))
    loop = _make_loop_with_broker([_op(), _msg("no backend was available")], no_backend_broker)
    result = loop.step("do something")
    assert result.operations[0].category == OutcomeCategory.BACKEND_UNAVAILABLE

    crashing_code = "raise ValueError('boom')"
    failing_loop = _make_loop([_op(consequence="high", artifact_code=crashing_code), _msg("that crashed")])
    failing_result = failing_loop.step("do something else")
    assert failing_result.operations[0].category == OutcomeCategory.EXECUTION_FAILED

    assert OutcomeCategory.BACKEND_UNAVAILABLE != OutcomeCategory.EXECUTION_FAILED


# ---- integrity rejection fails closed, never becomes a recoverable continuation input ------------

def test_integrity_rejected_fails_closed_not_recoverable():
    loop = _make_loop_with_broker([_op()], _IntegrityRejectingBroker())

    with pytest.raises(DecisionVerificationError):
        loop.step("do something")

    # the completion is still recorded for diagnostic completeness, but no "effect" entry is
    # fabricated, and the loop never called the model a second time to "recover"
    assert [entry["role"] for entry in loop.history] == ["user", "assistant"]


# ---- multiple operations: each independently dispatched, in order -------------------------------

def test_multiple_operations_are_each_independently_dispatched_in_order():
    op_a = _op(consequence="low", artifact_code="RESULT = 1")
    op_b = _op(consequence="high", artifact_code="print('b')")
    loop = _make_loop([op_a, op_b, _msg("done both")])

    result = loop.step("do two things")

    assert result.message == "done both"
    assert len(result.operations) == 2
    assert result.operations[0].execution_class == "same_process"
    assert result.operations[1].execution_class == "separate_process"
    assert all(o.category == OutcomeCategory.EXECUTED for o in result.operations)


# ---- hard per-turn operation bound ---------------------------------------------------------------

def test_hard_operation_bound_refuses_the_n_plus_first_operation_before_dispatch():
    gate = Gate(ConsequencePolicy())
    real_broker = Broker(gate=gate, executor=Executor(gate, backends={"same_process": SameProcessBackend(allow_root=True)}))
    counting_broker = _CountingRealBroker(real_broker)
    loop = _make_loop_with_broker(
        [_op(), _op(message="trying again")], counting_broker, max_operations_per_turn=1,
    )

    result = loop.step("do two things")

    assert counting_broker.call_count == 1  # the second operation never reached Broker.dispatch() at all
    assert result.exhausted is True
    assert len(result.operations) == 1
    assert result.message == "trying again"  # honestly shown, not fabricated or dropped


def test_operation_count_within_the_bound_is_not_refused():
    loop = _make_loop([_op(), _msg("done")], max_operations_per_turn=1)
    result = loop.step("do one thing")
    assert result.exhausted is False
    assert len(result.operations) == 1


def test_default_operation_bound_is_a_small_positive_integer():
    from siphonophore_harness.loop import DEFAULT_MAX_OPERATIONS_PER_TURN

    assert isinstance(DEFAULT_MAX_OPERATIONS_PER_TURN, int)
    assert DEFAULT_MAX_OPERATIONS_PER_TURN > 0


# ---- CognitiveLoop holding a delegated Authority (within scope) ----------------------------------

def test_loop_holding_a_delegated_authority_dispatches_through_it():
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    loop_b = CognitiveLoop(
        model=ScriptedModel([_op(), _msg("done")]), broker=broker,
        principal_id="agent-a.sub-agent-b", authority=authority_b,
    )

    result = loop_b.step("do the delegated subtask")
    assert result.operations[0].execution_class == "same_process"


# ---- last_message: reflects the most recent cycle's own parsed message ---------------------------

def test_last_message_reflects_the_most_recent_cycles_own_message():
    loop = _make_loop([_op(message="sure, doing that"), _msg("done")])
    loop.step("please run something")
    assert loop.last_message == "done"  # the FINAL cycle's message, not the intermediate one


def test_last_message_is_none_when_the_final_cycles_completion_has_no_message_field():
    loop = _make_loop([_op(message="sure, doing that"), _msg()])
    loop.step("please run something")
    assert loop.last_message is None


def test_last_message_does_not_leak_from_a_previous_turn():
    loop = _make_loop([_op(), _msg("first turn's message"), _op(), _msg("second turn's message")])
    loop.step("first")
    assert loop.last_message == "first turn's message"
    loop.step("second")
    assert loop.last_message == "second turn's message"  # not stale text from the first turn


def test_last_message_is_none_when_the_completion_fails_to_parse_at_all():
    hostile = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "token": "trust-me-bro"}})
    loop = _make_loop([hostile])
    with pytest.raises(IntentParseError):
        loop.step("do something")
    assert loop.last_message is None


# ---- turn contract: message-only turns never dispatch --------------------------------------------

def test_message_only_completion_never_calls_broker_dispatch():
    """The core acceptance test for the turn contract: a completion naming no operation at all
    must produce zero Broker.dispatch() calls -- and therefore zero Gate submissions and zero
    backend executions, since dispatch() is the only path to either."""
    broker = _CountingBroker()
    loop = _make_loop_with_broker([_msg("Good morning!")], broker)

    result = loop.step("Good Morning")

    assert broker.call_count == 0
    assert isinstance(result, TurnResult)
    assert result.operations == ()


def test_message_only_completion_returns_a_turn_result_with_the_message_and_no_operations():
    loop = _make_loop([_msg("The capital of Japan is Tokyo.")])

    result = loop.step("what is the capital of Japan?")

    assert result.message == "The capital of Japan is Tokyo."
    assert result.operations == ()
    assert result.exhausted is False


def test_empty_envelope_completion_is_also_message_only_with_zero_dispatch():
    broker = _CountingBroker()
    loop = _make_loop_with_broker([json.dumps({})], broker)

    result = loop.step("...")

    assert broker.call_count == 0
    assert result.operations == ()
    assert result.message is None


def test_message_only_turn_still_records_honest_history_not_a_fabricated_effect():
    completion = _msg("hello")
    loop = _make_loop([completion])

    loop.step("hi")

    roles = [entry["role"] for entry in loop.history]
    assert roles == ["user", "assistant", "effect"]
    assert loop.history[-1]["content"] == "no operation requested this turn"
    assert loop.history[1]["content"] == completion  # the raw completion stays available as context


# ---- model-context bound: OperationOutcome.detail is never mutated/truncated by it ----------------

def test_model_context_detail_is_bounded_while_operation_outcome_detail_is_not():
    large_output = "A" * 10_000
    loop = _make_loop([_op(artifact_code=f"print({large_output!r})"), _msg("done")])

    result = loop.step("print something large")

    full_detail = result.operations[0].detail["stdout"]
    assert len(full_detail) > 10_000  # the backend-capture bound (100,000 chars) left this untouched

    effect_entry = loop.history[2]["content"]  # user, assistant, effect(op1), assistant, effect(final)
    assert "truncated for model context" in effect_entry
    assert len(effect_entry) < len(full_detail)


# ---- model-boundary observability: last_completion/last_diagnostics survive a parse failure -----

def test_last_completion_is_set_even_when_the_completion_fails_to_parse_at_all():
    """The core observability gap this stage closes: previously, a completion that failed
    parse_turn() was never appended to history, so it was lost entirely -- unrecoverable even
    under --verbose. last_completion is set as soon as model.complete() returns, before
    parse_turn() is even called."""
    loop = _make_loop(["not valid json at all"])
    with pytest.raises(IntentParseError):
        loop.step("what is your operating context")
    assert loop.last_completion == "not valid json at all"


def test_last_completion_is_none_before_the_first_step():
    loop = _make_loop([])
    assert loop.last_completion is None


def test_last_completion_does_not_leak_from_a_previous_failed_step():
    completion = _msg("ok")
    loop = _make_loop(["not json", completion])
    with pytest.raises(IntentParseError):
        loop.step("first")
    assert loop.last_completion == "not json"
    loop.step("second")
    assert loop.last_completion == completion


def test_last_completion_reflects_the_most_recent_cycle_of_a_successful_operation_turn():
    final_completion = _msg("all done")
    loop = _make_loop([_op(), final_completion])
    loop.step("please run something")
    assert loop.last_completion == final_completion  # the most recent cycle, not the operation one


def test_last_diagnostics_is_none_for_a_model_that_provides_none():
    """ScriptedModel (used throughout this file) declares no last_diagnostics attribute --
    CognitiveLoop must not require one to exist, on either the success or the parse-failure
    path."""
    loop = _make_loop([_msg("hi")])
    loop.step("hi")
    assert loop.last_diagnostics is None

    failing_loop = _make_loop(["not json"])
    with pytest.raises(IntentParseError):
        failing_loop.step("hi")
    assert failing_loop.last_diagnostics is None
