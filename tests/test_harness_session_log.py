"""Tests for siphonophore_harness/session_log.py: EventLog -- the durable, append-oriented,
model-neutral JSONL session event stream (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md)."""
from __future__ import annotations

import json

import pytest

from siphonophore_harness.session_log import SCHEMA_VERSION, EventLog


def test_emit_returns_the_constructed_event_even_with_no_path():
    log = EventLog(path=None, session_id="s-1")
    event = log.emit("user.message", turn_id="t-1", content="hello")
    assert event["event"] == "user.message"
    assert event["session_id"] == "s-1"
    assert event["schema_version"] == SCHEMA_VERSION
    assert event["turn_id"] == "t-1"
    assert event["content"] == "hello"


def test_emit_with_no_path_writes_nothing_to_disk(tmp_path):
    log = EventLog(path=None, session_id="s-1")
    log.emit("user.message", turn_id="t-1", content="hello")
    assert list(tmp_path.iterdir()) == []


def test_emit_with_a_path_appends_one_jsonl_line_per_call(tmp_path):
    path = tmp_path / "session.jsonl"
    log = EventLog(path=path, session_id="s-1")
    log.emit("session.started", model="claude-x")
    log.emit("user.message", turn_id="t-1", content="hi")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["event"] == "session.started"
    assert second["event"] == "user.message"
    assert first["session_id"] == second["session_id"] == "s-1"


def test_session_id_is_auto_generated_when_not_given():
    log = EventLog(path=None)
    assert log.session_id  # non-empty
    other = EventLog(path=None)
    assert other.session_id != log.session_id


def test_creates_parent_directories_for_the_log_path(tmp_path):
    path = tmp_path / "nested" / "dir" / "session.jsonl"
    log = EventLog(path=path, session_id="s-1")
    log.emit("session.started")
    assert path.exists()


def test_forbidden_keys_are_dropped_not_logged(tmp_path):
    path = tmp_path / "session.jsonl"
    log = EventLog(path=path, session_id="s-1")
    log.emit("operation.result", intent_id="i-1", token="should-never-appear", api_key="sk-fake")
    line = json.loads(path.read_text().strip())
    assert "token" not in line
    assert "api_key" not in line
    assert line["intent_id"] == "i-1"


def test_long_free_text_fields_are_bounded(tmp_path):
    path = tmp_path / "session.jsonl"
    log = EventLog(path=path, session_id="s-1")
    huge = "A" * 10_000
    event = log.emit("model.response", turn_id="t-1", cycle_id="c-1", completion=huge)
    assert len(event["completion"]) < len(huge)
    assert "truncated for event log" in event["completion"]


def test_work_order_payload_is_not_truncated_even_if_large(tmp_path):
    """Self-containment is the whole point of a WorkOrder -- it is exempted from the generic
    per-field text bound, unlike an ordinary completion or reason string."""
    path = tmp_path / "session.jsonl"
    log = EventLog(path=path, session_id="s-1")
    long_prompt = "B" * 10_000
    work_order_dict = {"work_order_id": "wo-1", "status": "final", "objective": "x", "prompt": long_prompt}
    event = log.emit("work_order.finalized", turn_id="t-1", work_order=work_order_dict)
    assert event["work_order"]["prompt"] == long_prompt  # untouched, not truncated


def test_sink_matches_emit_shape_and_does_not_mutate_the_caller_dict(tmp_path):
    path = tmp_path / "session.jsonl"
    log = EventLog(path=path, session_id="s-1")
    original_event = {"event": "operation.requested", "turn_id": "t-1", "intent_id": "i-1"}
    original_copy = dict(original_event)
    log.sink(original_event)
    assert original_event == original_copy  # sink() never mutates its argument

    line = json.loads(path.read_text().strip())
    assert line["event"] == "operation.requested"
    assert line["session_id"] == "s-1"
    assert line["intent_id"] == "i-1"


def test_not_thread_safe_is_documented_not_asserted():
    """Documentation-only check: confirms the disclosed single-caller-at-a-time assumption is
    stated in the module's own docstring, matching SameProcessBackend's own precedent for
    disclosing rather than silently assuming concurrency safety."""
    import siphonophore_harness.session_log as session_log_module

    assert "not thread-safe" in session_log_module.EventLog.__doc__.lower()


# ---- end-to-end: a real CognitiveLoop turn produces a coherent JSONL stream -----------------------

def test_end_to_end_turn_produces_a_coherent_correlated_jsonl_stream(tmp_path):
    import json as _json

    from siphonophore_core.execution import Executor, SameProcessBackend
    from siphonophore_core.mediation import Gate
    from siphonophore_core.policy import ConsequencePolicy
    from siphonophore_harness.broker import Broker
    from siphonophore_harness.loop import CognitiveLoop
    from siphonophore_harness.model import ScriptedModel

    path = tmp_path / "session.jsonl"
    log = EventLog(path=path, session_id="s-e2e")

    gate = Gate(ConsequencePolicy())
    broker = Broker(gate=gate, executor=Executor(gate, backends={"same_process": SameProcessBackend(allow_root=True)}))
    completions = [
        _json.dumps({"operation": {"kind": "run_artifact", "consequence": "low", "artifact_code": "pass"}}),
        _json.dumps({"message": "done"}),
    ]
    loop = CognitiveLoop(model=ScriptedModel(completions), broker=broker, principal_id="alice", event_sink=log.sink)

    loop.step("do something")

    lines = [_json.loads(line) for line in path.read_text().strip().splitlines()]
    event_types = [line["event"] for line in lines]
    assert event_types == [
        "user.message", "model.response", "operation.requested", "mediation.decision",
        "operation.result", "model.continuation", "model.response", "turn.completed",
    ]

    turn_ids = {line["turn_id"] for line in lines if "turn_id" in line}
    assert len(turn_ids) == 1

    operation_lines = [line for line in lines if line["event"] in ("operation.requested", "mediation.decision", "operation.result")]
    intent_ids = {line["intent_id"] for line in operation_lines}
    assert len(intent_ids) == 1

    assert all(line["session_id"] == "s-e2e" for line in lines)
    assert all(line["schema_version"] == SCHEMA_VERSION for line in lines)

    decision_line = next(line for line in lines if line["event"] == "mediation.decision")
    assert decision_line["category"] == "executed"
    assert decision_line["execution_class"] == "same_process"

    result_line = next(line for line in lines if line["event"] == "operation.result")
    assert result_line["category"] == "executed"

    completed_line = lines[-1]
    assert completed_line["operation_count"] == 1
    assert completed_line["exhausted"] is False
