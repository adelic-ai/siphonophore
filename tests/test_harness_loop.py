"""Tests for CognitiveLoop.step(): the full prompt -> completion -> parse intent -> dispatch ->
feed back cycle, including the case that matters most for DESIGN.md section 7's proof -- a hostile
completion that tries to describe more authority than it should get, or to smuggle fields outside
Intent's schema, is refused by parse_intent/Broker exactly the same way any other bad input is,
because the loop has no other way to produce an effect."""
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


def test_step_dispatches_the_parsed_intent_and_returns_the_effect():
    completion = json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    loop = _make_loop([completion])
    effect = loop.step("please run something")
    assert effect.execution_class == "same_process"


def test_step_feeds_the_effect_back_into_history_for_the_next_turn():
    completion = json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    loop = _make_loop([completion])
    loop.step("please run something")

    roles = [entry["role"] for entry in loop.history]
    assert roles == ["user", "assistant", "effect"]
    assert "same_process" in loop.history[-1]["content"]


def test_second_step_sees_first_steps_history():
    completion = json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    loop = _make_loop([completion, completion])
    loop.step("first")
    history_before_second_call = list(loop.history)
    loop.step("second")
    # the model's second complete() call was handed the accumulated history from the first turn
    assert loop.history[: len(history_before_second_call)] == history_before_second_call


def test_hostile_completion_naming_unknown_fields_is_refused_before_any_dispatch():
    """A completion that tries to smuggle a pre-authorized-looking field (e.g. "token") past the
    Gate never gets the chance -- parse_intent() rejects it outright, and nothing resembling an
    Effect is ever produced."""
    hostile = json.dumps({"kind": "run_artifact", "consequence": "low", "token": "trust-me-bro"})
    loop = _make_loop([hostile])
    with pytest.raises(IntentParseError):
        loop.step("do something")
    # the user turn is recorded, but no assistant/effect turn was appended -- the loop did not
    # pretend the dispatch happened
    assert [entry["role"] for entry in loop.history] == ["user"]


def test_completion_requesting_a_denied_kind_is_refused_by_the_gate_not_silently_run():
    denied = json.dumps({"kind": "definitely_not_allowed", "consequence": "low"})
    loop = _make_loop([denied])
    with pytest.raises(GateViolation):
        loop.step("do something forbidden")


def test_last_message_set_from_the_parsed_completion():
    completion = json.dumps({"message": "sure, doing that", "kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    loop = _make_loop([completion])
    loop.step("please run something")
    assert loop.last_message == "sure, doing that"


def test_last_message_none_when_the_completion_has_no_message_field():
    completion = json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    loop = _make_loop([completion])
    loop.step("please run something")
    assert loop.last_message is None


def test_last_message_does_not_leak_from_a_previous_turn():
    with_message = json.dumps({"message": "first turn's message", "kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    without_message = json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
    loop = _make_loop([with_message, without_message])
    loop.step("first")
    assert loop.last_message == "first turn's message"
    loop.step("second")
    assert loop.last_message is None  # not stale text from the first turn


def test_last_message_is_set_even_when_the_gate_refuses_the_dispatch():
    """message is extracted before dispatch is attempted -- a refused intent still lets the human
    see what the model said, even though nothing it described actually happened."""
    denied = json.dumps({"message": "I'll try this forbidden thing", "kind": "definitely_not_allowed", "consequence": "low"})
    loop = _make_loop([denied])
    with pytest.raises(GateViolation):
        loop.step("do something forbidden")
    assert loop.last_message == "I'll try this forbidden thing"


def test_last_message_is_none_when_the_completion_fails_to_parse_at_all():
    hostile = json.dumps({"kind": "run_artifact", "consequence": "low", "token": "trust-me-bro"})
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
    completion = json.dumps({"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"})
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
    out_of_scope_completion = json.dumps({"kind": "write_file", "consequence": "low", "artifact_code": "pass"})
    loop_b = CognitiveLoop(model=ScriptedModel([out_of_scope_completion]), broker=broker, principal_id="agent-a.sub-agent-b", authority=authority_b)

    with pytest.raises(GateViolation):
        loop_b.step("try something outside what was delegated")
