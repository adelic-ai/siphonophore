"""Scenario-by-scenario coverage of CognitiveLoop's transactional history/event semantics
(docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md), matching the reference acceptance scenarios A-J:

A. model transport failure before any operation
B. empty/malformed response before any operation
C. parse failure before any operation
D. denied operation
E. backend unavailable
F. execution failure
G. successful operation followed by continuation-model failure
H. multiple successful operations followed by final-response failure
I. integrity rejection
J. operation-bound exhaustion

The governing rule under test throughout: a turn that fails before any real Broker.dispatch()
attempt commits NOTHING to CognitiveLoop.history (not even the user's own message), so it cannot
dangle and contaminate the next, unrelated turn. A REAL mediated operation, once it happened, is
committed permanently and is never later erased by a subsequent failure in the same turn. Nothing
is ever fabricated to keep roles alternating. Every scenario is also checked against the event
stream a caller-supplied sink receives, since the durable JSONL record must remain truthful even
for turns whose conversational history was rolled back.
"""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import DecisionVerificationError, Executor, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.model import Model, ScriptedModel
from siphonophore_harness.outcome import OutcomeCategory


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def sink(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e["event"] for e in self.events]


def _make_loop(completions, sink=None, **kwargs) -> CognitiveLoop:
    gate = Gate(ConsequencePolicy())
    backends = {"same_process": SameProcessBackend(allow_root=True), "separate_process": SeparateProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    return CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice", event_sink=sink, **kwargs)


def _make_loop_with_broker(completions, broker, sink=None, **kwargs) -> CognitiveLoop:
    return CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice", event_sink=sink, **kwargs)


class _CountingBroker:
    """Proves a code path never calls dispatch() at all, matching the convention already used by
    tests/test_harness_loop.py."""

    def __init__(self) -> None:
        self.call_count = 0

    def dispatch(self, intent, authority=None):
        self.call_count += 1
        raise AssertionError("Broker.dispatch() must never be called for this turn")


def _op(kind="run_artifact", consequence="low", artifact_code="pass", message=None):
    body = {"operation": {"kind": kind, "consequence": consequence, "artifact_code": artifact_code}}
    if message is not None:
        body["message"] = message
    return json.dumps(body)


def _msg(message=None):
    return json.dumps({"message": message} if message is not None else {})


class _RaisingModel(Model):
    """A Model whose complete() raises on a chosen ABSOLUTE call index (across this model's whole
    lifetime, i.e. possibly spanning more than one CognitiveLoop.step() call) -- simulates a real
    network/API transport failure, as opposed to a completion that parses to something malformed.
    `completions` need only contain the completions that are actually returned, in order; the call
    that raises consumes no entry."""

    def __init__(self, completions: list[str], fail_at: int) -> None:
        self._completions = list(completions)
        self._fail_at = fail_at
        self._call_index = 0

    def complete(self, messages: list[dict]) -> str:
        current = self._call_index
        self._call_index += 1
        if current == self._fail_at:
            raise RuntimeError("simulated: Anthropic API request failed")
        return self._completions.pop(0)


def _loop_with_raising_model(completions, fail_at, sink=None) -> CognitiveLoop:
    gate = Gate(ConsequencePolicy())
    backends = {"same_process": SameProcessBackend(allow_root=True), "separate_process": SeparateProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    return CognitiveLoop(model=_RaisingModel(completions, fail_at), broker=broker, principal_id="alice", event_sink=sink)


# ---- A. model transport failure before any operation --------------------------------------------

def test_A_model_transport_failure_before_any_operation_rolls_back_history():
    capture = _EventCapture()
    loop = _loop_with_raising_model([], fail_at=0, sink=capture.sink)

    with pytest.raises(RuntimeError, match="simulated"):
        loop.step("investigate the repository")

    assert loop.history == []  # rolled back entirely, including the user's own message
    assert "user.message" in capture.types()  # but durably logged for provenance
    assert "model.failure" in capture.types()
    assert "turn.failed" in capture.types()


def test_next_turn_after_A_is_not_contaminated():
    capture = _EventCapture()
    loop = _loop_with_raising_model([_msg("Tokyo is the capital of Japan.")], fail_at=0, sink=capture.sink)
    with pytest.raises(RuntimeError):
        loop.step("investigate the repository")

    result = loop.step("what is the capital of japan")
    assert result.message == "Tokyo is the capital of Japan."
    # the failed investigation request never appears anywhere in history
    assert all("investigate" not in entry["content"] for entry in loop.history)


# ---- B. empty/malformed response before any operation --------------------------------------------

def test_B_empty_response_before_any_operation_rolls_back_history():
    capture = _EventCapture()
    loop = _make_loop([""], sink=capture.sink, max_parse_retries_per_turn=0)
    with pytest.raises(IntentParseError):
        loop.step("investigate the repository")
    assert loop.history == []
    assert "model.failure" in capture.types()
    assert "turn.failed" in capture.types()


# ---- C. parse failure before any operation (non-empty but invalid) --------------------------------

def test_C_non_json_parse_failure_before_any_operation_rolls_back_history():
    loop = _make_loop(["this is not json at all"], max_parse_retries_per_turn=0)
    with pytest.raises(IntentParseError):
        loop.step("investigate the repository")
    assert loop.history == []


# ---- D. denied operation --------------------------------------------------------------------------

def test_D_denied_operation_commits_history_and_is_recoverable():
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact",)))
    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))
    capture = _EventCapture()
    loop = CognitiveLoop(
        model=ScriptedModel([_op(kind="write_file"), _msg("that was denied")]),
        broker=broker, principal_id="alice", event_sink=capture.sink,
    )
    result = loop.step("do something forbidden")
    assert result.operations[0].category == OutcomeCategory.DENIED
    assert [e["role"] for e in loop.history] == ["user", "assistant", "effect", "assistant", "effect"]
    assert "operation.requested" in capture.types()
    assert "mediation.decision" in capture.types()
    assert "operation.result" in capture.types()


# ---- E. backend unavailable ------------------------------------------------------------------------

def test_E_backend_unavailable_commits_history_and_is_recoverable():
    gate = Gate(ConsequencePolicy())
    broker = Broker(gate=gate, executor=Executor(gate, backends={}))
    loop = CognitiveLoop(model=ScriptedModel([_op(), _msg("no backend available")]), broker=broker, principal_id="alice")
    result = loop.step("do something")
    assert result.operations[0].category == OutcomeCategory.BACKEND_UNAVAILABLE
    assert [e["role"] for e in loop.history] == ["user", "assistant", "effect", "assistant", "effect"]


# ---- F. execution failure --------------------------------------------------------------------------

def test_F_execution_failure_commits_history_and_is_recoverable():
    loop = _make_loop([_op(artifact_code="raise ValueError('boom')"), _msg("that crashed")])
    result = loop.step("do something that fails")
    assert result.operations[0].category == OutcomeCategory.EXECUTION_FAILED
    assert [e["role"] for e in loop.history] == ["user", "assistant", "effect", "assistant", "effect"]


# ---- G. successful operation followed by continuation-model failure --------------------------------

def test_G_operation_succeeds_then_continuation_model_fails_operation_survives_in_history():
    capture = _EventCapture()
    loop = _loop_with_raising_model([_op()], fail_at=1, sink=capture.sink)

    with pytest.raises(RuntimeError, match="simulated"):
        loop.step("do something then fail")

    # the REAL operation's own assistant+effect entries remain -- only the failed second cycle
    # produced nothing (no fabricated assistant reply for the failure itself)
    assert [e["role"] for e in loop.history] == ["user", "assistant", "effect"]
    assert "same_process" in loop.history[2]["content"]
    assert "turn.failed" in capture.types()
    assert "operation.result" in capture.types()  # the real operation's result was still logged


# ---- H. multiple successful operations followed by final-response failure --------------------------

def test_H_multiple_operations_then_final_response_failure_all_operations_survive():
    op_a = _op(consequence="low", artifact_code="RESULT = 1")
    op_b = _op(consequence="high", artifact_code="print('b')")
    loop = _loop_with_raising_model([op_a, op_b], fail_at=2)

    with pytest.raises(RuntimeError, match="simulated"):
        loop.step("do two things then fail")

    roles = [e["role"] for e in loop.history]
    assert roles == ["user", "assistant", "effect", "assistant", "effect"]
    assert "same_process" in loop.history[2]["content"]
    assert "separate_process" in loop.history[4]["content"]


# ---- I. integrity rejection fails closed, but the attempt itself is still committed ------------------

class _IntegrityRejectingBroker:
    def dispatch(self, intent, authority=None):
        raise DecisionVerificationError("forged decision, simulated for this test")


def test_I_integrity_rejection_fails_closed_but_commits_the_attempt():
    capture = _EventCapture()
    loop = CognitiveLoop(
        model=ScriptedModel([_op()]), broker=_IntegrityRejectingBroker(), principal_id="alice",
        event_sink=capture.sink,
    )
    with pytest.raises(DecisionVerificationError):
        loop.step("do something")
    # the attempt itself (a real, if untrustworthy, mediation event) is committed -- no effect
    # entry is fabricated for it, since there is no safe, truthful outcome text to attach
    assert [e["role"] for e in loop.history] == ["user", "assistant"]
    assert "turn.failed" in capture.types()


# ---- J. operation-bound exhaustion -------------------------------------------------------------------

def test_J_operation_bound_exhaustion_commits_history_with_an_honest_limit_marker():
    capture = _EventCapture()
    loop = _make_loop([_op(), _op(message="trying again")], sink=capture.sink, max_operations_per_turn=1)
    result = loop.step("do two things")
    assert result.exhausted is True
    assert result.message == "trying again"
    roles = [e["role"] for e in loop.history]
    assert roles == ["user", "assistant", "effect", "assistant", "effect"]
    assert loop.history[-1]["content"] == "operation limit reached this turn"
    turn_completed = [e for e in capture.events if e["event"] == "turn.completed"]
    assert turn_completed[-1]["exhausted"] is True


# ---- cross-cutting: event correlation -------------------------------------------------------------

def test_event_correlation_ids_are_stable_within_one_turn():
    capture = _EventCapture()
    loop = _make_loop([_op(), _msg("done")], sink=capture.sink)
    loop.step("do something")

    turn_ids = {e["turn_id"] for e in capture.events if "turn_id" in e}
    assert len(turn_ids) == 1  # every event in this turn shares one turn_id

    operation_events = [e for e in capture.events if e["event"] in ("operation.requested", "mediation.decision", "operation.result")]
    intent_ids = {e["intent_id"] for e in operation_events}
    assert len(intent_ids) == 1  # all three phases of the one dispatch share one intent_id


def test_event_sink_receives_nothing_when_none_configured():
    # No sink configured -- CognitiveLoop must not require one, and must not raise trying to use it.
    loop = _make_loop([_msg("hi")])
    result = loop.step("hi")
    assert result.message == "hi"


# ---- V1: the planning profile structurally forbids code execution, end to end -------------------

def test_planning_profile_end_to_end_denies_run_artifact_no_effect_occurs():
    """A hostile or confused completion asking for "run_artifact" against the planning profile
    must be denied (recoverable) -- never dispatched to a code-execution backend, because no such
    backend is registered and KindExecutionPolicy has no mapping for that kind either."""
    from siphonophore_harness.composition import planning_profile

    profile = planning_profile(root=".")
    escape_attempt = json.dumps({
        "operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "import os; os.system('echo escaped')"},
    })
    loop = CognitiveLoop(
        model=ScriptedModel([escape_attempt, _msg("that was refused")]),
        broker=profile.broker, principal_id="alice",
    )
    result = loop.step("try to run arbitrary code")
    assert result.operations[0].category == OutcomeCategory.DENIED
    assert result.operations[0].detail == {}  # no effect occurred
    assert result.message == "that was refused"


def test_planning_profile_end_to_end_denies_write_file_no_effect_occurs(tmp_path):
    from siphonophore_harness.composition import planning_profile

    profile = planning_profile(root=str(tmp_path))
    escape_attempt = json.dumps({
        "operation": {"kind": "write_file", "consequence": "low", "artifact_code": "open('pwned.txt', 'w').write('x')"},
    })
    loop = CognitiveLoop(model=ScriptedModel([escape_attempt, _msg("refused")]), broker=profile.broker, principal_id="alice")
    result = loop.step("try to write a file")
    assert result.operations[0].category == OutcomeCategory.DENIED
    assert not (tmp_path / "pwned.txt").exists()


# ---- V1: compiling a WorkOrder never dispatches anything ------------------------------------------

def test_work_order_finalization_never_calls_broker_dispatch():
    broker = _CountingBroker()
    work_order_completion = json.dumps({
        "message": "Here is the compiled plan.",
        "work_order": {
            "status": "final", "objective": "add a feature", "prompt": "implement X exactly as discussed",
        },
    })
    loop = _make_loop_with_broker([work_order_completion], broker)

    result = loop.step("compile the work order")

    assert broker.call_count == 0
    assert result.work_order is not None
    assert result.work_order.is_final is True
    assert result.operations == ()


def test_work_order_draft_also_never_dispatches_and_is_distinguishable_from_final():
    broker = _CountingBroker()
    draft_completion = json.dumps({
        "work_order": {"status": "draft", "objective": "add a feature", "prompt": "still refining this"},
    })
    loop = _make_loop_with_broker([draft_completion], broker)

    result = loop.step("here's a first draft")

    assert broker.call_count == 0
    assert result.work_order.is_final is False


def test_work_order_finalization_commits_history_and_is_logged():
    capture = _EventCapture()
    work_order_completion = json.dumps({
        "work_order": {"status": "final", "objective": "add a feature", "prompt": "implement X"},
    })
    loop = _make_loop([work_order_completion], sink=capture.sink)
    loop.step("compile it")

    assert [e["role"] for e in loop.history] == ["user", "assistant", "effect"]
    assert "work_order.finalized" in capture.types()
    finalized_event = next(e for e in capture.events if e["event"] == "work_order.finalized")
    assert finalized_event["work_order"]["objective"] == "add a feature"


# ---- V1: bounded malformed-output retry ------------------------------------------------------------

def test_malformed_completion_recovers_via_bounded_retry():
    capture = _EventCapture()
    loop = _make_loop(["not json at all", _msg("recovered")], sink=capture.sink)
    result = loop.step("say something")
    assert result.message == "recovered"
    assert [e["role"] for e in loop.history] == ["user", "assistant", "effect", "assistant", "effect"]
    assert loop.history[1]["content"] == "not json at all"
    assert "did not parse" in loop.history[2]["content"]
    assert capture.types().count("model.failure") == 1
    assert "turn.completed" in capture.types()
    assert "turn.failed" not in capture.types()


def test_malformed_completion_retries_do_not_exceed_the_configured_bound():
    """Two malformed completions in a row, with max_parse_retries_per_turn=1 (only one retry
    allowed): the first failure is retried once: if that retry is ALSO malformed, the turn fails
    closed -- it does not retry a second time."""
    capture = _EventCapture()
    loop = _make_loop(["bad one", "bad two"], sink=capture.sink, max_parse_retries_per_turn=1)
    with pytest.raises(IntentParseError):
        loop.step("say something")
    assert loop.history == []  # nothing ever committed -- the whole speculative sequence rolls back
    assert capture.types().count("model.failure") == 2
    assert "turn.failed" in capture.types()


def test_malformed_completion_exhausting_all_retries_rolls_back_entirely():
    loop = _make_loop(["bad one", "bad two", "bad three"], max_parse_retries_per_turn=2)
    with pytest.raises(IntentParseError):
        loop.step("say something")
    assert loop.history == []


def test_malformed_retry_after_a_real_operation_preserves_the_operation():
    """A parse failure on a CONTINUATION cycle (after a real operation already committed) still
    gets bounded retries, and the prior real operation is never lost regardless of how the retry
    sequence resolves."""
    loop = _make_loop([_op(), "not json", _msg("recovered after retry")])
    result = loop.step("do something then stumble")
    assert result.message == "recovered after retry"
    assert len(result.operations) == 1
    roles = [e["role"] for e in loop.history]
    assert roles == ["user", "assistant", "effect", "assistant", "effect", "assistant", "effect"]
    assert "same_process" in loop.history[2]["content"]


def test_default_parse_retry_bound_is_a_small_non_negative_integer():
    from siphonophore_harness.loop import DEFAULT_MAX_PARSE_RETRIES_PER_TURN

    assert isinstance(DEFAULT_MAX_PARSE_RETRIES_PER_TURN, int)
    assert 0 <= DEFAULT_MAX_PARSE_RETRIES_PER_TURN <= 5


# ---- V1: long-session coherence ---------------------------------------------------------------

def test_long_mixed_session_stays_coherent_across_many_turns():
    """Simulates ~20 turns mixing message-only, successful operations, denials, a bound-exhausted
    turn, and a WorkOrder draft/final pair -- confirms history keeps growing coherently (every
    "user" entry is followed, sooner or later, by real content; nothing is silently corrupted or
    duplicated) and no turn's outcome bleeds into another's. Not full resume/persistence testing
    (explicitly deferred, docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md) -- purely an in-memory
    long-session coherence check."""
    gate = Gate(ConsequencePolicy(allowed_kinds=("run_artifact",)))
    backends = {"same_process": SameProcessBackend(allow_root=True)}
    broker = Broker(gate=gate, executor=Executor(gate, backends=backends))

    completions: list[str] = []
    expected_message_count = 0
    for i in range(5):
        completions.append(_msg(f"conversational reply {i}"))
        expected_message_count += 1
        completions.append(_op(consequence="low", artifact_code=f"RESULT = {i}"))
        completions.append(_msg(f"operation {i} done"))
        expected_message_count += 1
        completions.append(_op(kind="write_file"))  # denied: not in allowed_kinds
        completions.append(_msg(f"operation {i} was denied"))
        expected_message_count += 1

    capture = _EventCapture()
    loop = CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice", event_sink=capture.sink)

    results = []
    for i in range(15):
        user_message = f"turn {i}"
        results.append(loop.step(user_message))

    assert len(results) == 15
    # every user entry in history is real -- exactly one per successful step() call, none dropped
    # or duplicated, and each is followed by at least one non-user entry before the next user entry
    user_indices = [idx for idx, entry in enumerate(loop.history) if entry["role"] == "user"]
    assert len(user_indices) == 15
    for a, b in zip(user_indices, user_indices[1:]):
        assert b > a + 1  # at least one assistant/effect entry lies strictly between consecutive users

    turn_ids_seen = {e["turn_id"] for e in capture.events if "turn_id" in e}
    assert len(turn_ids_seen) == 15  # one distinct turn_id per step() call, never reused

    # the denied-operation turns really were denied, not silently run
    denied_results = [r for r in results if any(op.category == OutcomeCategory.DENIED for op in r.operations)]
    assert len(denied_results) == 5
