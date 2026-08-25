"""Tests for CognitiveLoop.step(): the full prompt -> completion -> parse intent -> dispatch ->
feed back cycle, including the case that matters most for DESIGN.md section 7's proof -- a hostile
completion that tries to describe more authority than it should get, or to smuggle fields outside
Intent's schema, is refused by parse_intent/Broker exactly the same way any other bad input is,
because the loop has no other way to produce an effect."""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import Executor
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.model import ScriptedModel


def _make_loop(completions: list[str]) -> CognitiveLoop:
    gate = Gate(ConsequencePolicy())
    broker = Broker(gate=gate, executor=Executor(gate))
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
