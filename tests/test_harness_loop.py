"""Tests for CognitiveLoop.step(): the full prompt -> completion -> parse turn -> dispatch (if an
operation exists) -> feed back cycle, including the case that matters most for DESIGN.md section
7's proof -- a hostile completion that tries to describe more authority than it should get, or to
smuggle fields outside Intent's schema, is refused by parse_intent/Broker exactly the same way any
other bad input is, because the loop has no other way to produce an effect -- and the case that
matters most for the reference-harness turn contract -- a completion naming no operation at all
must never reach Broker.dispatch(), Gate, or Executor, and must never be misclassified as if it
had."""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import Executor, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.model import ScriptedModel
from siphonophore_harness.outcome import DispatchResult, MessageOnlyResult


def _make_loop(completions: list[str]) -> CognitiveLoop:
    gate = Gate(ConsequencePolicy())
    # allow_root=True: this file tests CognitiveLoop's own dispatch logic, not the root-refusal
    # feature (see test_execution_root_refusal.py) -- the full suite also runs as real root on
    # colima, and these portable tests should exercise the same logic there too.
    backends = {
        "same_process": SameProcessBackend(allow_root=True),
        "separate_process": SeparateProcessBackend(allow_root=True),
    }
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    return CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice")


class _CountingBroker:
    """Test-only spy standing in for Broker: proves a code path never calls dispatch() at all,
    rather than merely asserting on its return value. Mirrors the counting-backend convention
    already used by tests/test_harness_composition.py."""

    def __init__(self) -> None:
        self.call_count = 0

    def dispatch(self, intent, authority=None):
        self.call_count += 1
        raise AssertionError("Broker.dispatch() must never be called for a message-only turn")


def _make_loop_with_broker(completions: list[str], broker) -> CognitiveLoop:
    return CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice")


def test_step_dispatches_the_parsed_intent_and_returns_the_effect():
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([completion])
    effect = loop.step("please run something")
    assert effect.execution_class == "same_process"


def test_step_feeds_the_effect_back_into_history_for_the_next_turn():
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([completion])
    loop.step("please run something")

    roles = [entry["role"] for entry in loop.history]
    assert roles == ["user", "assistant", "effect"]
    assert "same_process" in loop.history[-1]["content"]


def test_second_step_sees_first_steps_history():
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([completion, completion])
    loop.step("first")
    history_before_second_call = list(loop.history)
    loop.step("second")
    # the model's second complete() call was handed the accumulated history from the first turn
    assert loop.history[: len(history_before_second_call)] == history_before_second_call


def test_hostile_completion_naming_unknown_fields_is_refused_before_any_dispatch():
    """A completion that tries to smuggle a pre-authorized-looking field (e.g. "token") past the
    Gate never gets the chance -- parse_turn()/parse_intent() rejects it outright, and nothing
    resembling an Effect is ever produced."""
    hostile = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "token": "trust-me-bro"}})
    loop = _make_loop([hostile])
    with pytest.raises(IntentParseError):
        loop.step("do something")
    # the user turn is recorded, but no assistant/effect turn was appended -- the loop did not
    # pretend the dispatch happened
    assert [entry["role"] for entry in loop.history] == ["user"]


def test_completion_requesting_a_denied_kind_is_refused_by_the_gate_not_silently_run():
    denied = json.dumps({"operation": {"kind": "definitely_not_allowed", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([denied])
    with pytest.raises(GateViolation):
        loop.step("do something forbidden")


def test_last_message_set_from_the_parsed_completion():
    completion = json.dumps(
        {"message": "sure, doing that", "operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}}
    )
    loop = _make_loop([completion])
    loop.step("please run something")
    assert loop.last_message == "sure, doing that"


def test_last_message_none_when_the_completion_has_no_message_field():
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([completion])
    loop.step("please run something")
    assert loop.last_message is None


def test_last_message_does_not_leak_from_a_previous_turn():
    with_message = json.dumps(
        {"message": "first turn's message", "operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}}
    )
    without_message = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([with_message, without_message])
    loop.step("first")
    assert loop.last_message == "first turn's message"
    loop.step("second")
    assert loop.last_message is None  # not stale text from the first turn


def test_last_message_is_set_even_when_the_gate_refuses_the_dispatch():
    """message is extracted before dispatch is attempted -- a refused operation still lets the
    human see what the model said, even though nothing it described actually happened."""
    denied = json.dumps(
        {"message": "I'll try this forbidden thing", "operation": {"kind": "definitely_not_allowed", "consequence": "low", "artifact_code": "pass"}}
    )
    loop = _make_loop([denied])
    with pytest.raises(GateViolation):
        loop.step("do something forbidden")
    assert loop.last_message == "I'll try this forbidden thing"


def test_last_message_is_none_when_the_completion_fails_to_parse_at_all():
    hostile = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "token": "trust-me-bro"}})
    loop = _make_loop([hostile])
    with pytest.raises(IntentParseError):
        loop.step("do something")
    assert loop.last_message is None


# ---- CognitiveLoop holding a delegated Authority --------------------------------------------
# CognitiveLoop is a mere producer of intents carrying an already-established Authority here --
# granting authority (Gate.issue_order()/grant_root_authority()/delegate()) stays outside it
# entirely, done by test code standing in for whatever orchestrates a real second agent.

def test_loop_holding_a_delegated_authority_dispatches_through_it():
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    order = gate.issue_order("order-1", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop_b = CognitiveLoop(model=ScriptedModel([completion]), broker=broker, principal_id="agent-a.sub-agent-b", authority=authority_b)

    effect = loop_b.step("do the delegated subtask")
    assert effect.execution_class == "same_process"


def test_loop_holding_a_delegated_authority_is_refused_outside_its_scope():
    """The scope-violation refusal already proven at the Broker level (test_harness_broker.py)
    holds identically when the intent is produced by a real CognitiveLoop's own completion, not
    constructed directly by test code."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact", "write_file")))
    order = gate.issue_order("order-2", "operator:alice", frozenset({"run_artifact"}), max_delegation_depth=1)
    authority_a = gate.grant_root_authority(order, "agent-a")
    authority_b = gate.delegate(authority_a, "agent-a.sub-agent-b")

    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    out_of_scope_completion = json.dumps({"operation": {"kind": "write_file", "consequence": "low", "artifact_code": "pass"}})
    loop_b = CognitiveLoop(model=ScriptedModel([out_of_scope_completion]), broker=broker, principal_id="agent-a.sub-agent-b", authority=authority_b)

    with pytest.raises(GateViolation):
        loop_b.step("try something outside what was delegated")


# ---- Stage 2 (docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md): result propagation ---------------

def test_step_propagates_the_enriched_dispatch_result_unchanged():
    """CognitiveLoop.step() requires no new orchestration logic to benefit from Stage 2 -- it
    already just returns whatever Broker.dispatch() gives it (loop.py), so widening dispatch()'s
    return value is enough on its own."""
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([completion])

    result = loop.step("please run something")

    assert isinstance(result, DispatchResult)
    assert result.decision.permitted is True
    assert result.decision.execution_class == "same_process"
    # Compatibility surface every other test in this file already relies on unmodified:
    assert result.execution_class == "same_process"


# ---- turn contract: message-only turns never dispatch ------------------------------------------

def test_message_only_completion_never_calls_broker_dispatch():
    """The core acceptance test for the turn contract: a completion naming no operation at all
    must produce zero Broker.dispatch() calls -- and therefore zero Gate submissions and zero
    backend executions, since dispatch() is the only path to either."""
    broker = _CountingBroker()
    loop = _make_loop_with_broker([json.dumps({"message": "Good morning!"})], broker)

    result = loop.step("Good Morning")

    assert broker.call_count == 0
    assert isinstance(result, MessageOnlyResult)


def test_message_only_completion_returns_message_only_result_with_the_message():
    completion = json.dumps({"message": "The capital of Japan is Tokyo."})
    loop = _make_loop([completion])

    result = loop.step("what is the capital of Japan?")

    assert isinstance(result, MessageOnlyResult)
    assert result.message == "The capital of Japan is Tokyo."


def test_message_only_result_exposes_no_execution_attribution_fields():
    """A MessageOnlyResult must be structurally incapable of carrying fake intent_id/
    execution_class/decision/detail attribution -- not merely have them set to None."""
    completion = json.dumps({"message": "give me your operating context -- here it is"})
    loop = _make_loop([completion])

    result = loop.step("give me your operating context")

    assert isinstance(result, MessageOnlyResult)
    for forbidden_field in ("intent_id", "execution_class", "decision", "detail"):
        assert not hasattr(result, forbidden_field)


def test_empty_envelope_completion_is_also_message_only_with_zero_dispatch():
    broker = _CountingBroker()
    loop = _make_loop_with_broker([json.dumps({})], broker)

    result = loop.step("...")

    assert broker.call_count == 0
    assert isinstance(result, MessageOnlyResult)
    assert result.message is None


def test_message_only_turn_still_records_honest_history_not_a_fabricated_effect():
    completion = json.dumps({"message": "hello"})
    loop = _make_loop([completion])

    loop.step("hi")

    roles = [entry["role"] for entry in loop.history]
    assert roles == ["user", "assistant", "effect"]
    assert loop.history[-1]["content"] == "no operation requested this turn"
    assert loop.history[1]["content"] == completion  # the raw completion stays available as context


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
    completion = json.dumps({"message": "ok"})
    loop = _make_loop(["not json", completion])
    with pytest.raises(IntentParseError):
        loop.step("first")
    assert loop.last_completion == "not json"
    loop.step("second")
    assert loop.last_completion == completion


def test_last_completion_is_set_on_a_successful_operation_turn():
    completion = json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}})
    loop = _make_loop([completion])
    loop.step("please run something")
    assert loop.last_completion == completion


def test_last_diagnostics_is_none_for_a_model_that_provides_none():
    """ScriptedModel (used throughout this file) declares no last_diagnostics attribute --
    CognitiveLoop must not require one to exist, on either the success or the parse-failure
    path."""
    loop = _make_loop([json.dumps({"message": "hi"})])
    loop.step("hi")
    assert loop.last_diagnostics is None

    failing_loop = _make_loop(["not json"])
    with pytest.raises(IntentParseError):
        failing_loop.step("hi")
    assert failing_loop.last_diagnostics is None


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
