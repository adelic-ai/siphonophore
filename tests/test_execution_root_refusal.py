"""Tests for SameProcessBackend/SeparateProcessBackend's refusal to run while the broker is
euid 0 (root) -- the fix for the finding that these backends previously inherited whatever
privilege the broker had with no resistance, so a broker running as root (needed for the
uid_cgroup tiers) would hand a "low consequence" intent full root, zero isolation.

Portable tests here fake os.geteuid() via monkeypatch -- the refusal logic itself needs no real
root to verify. test_execution_root_refusal_linux.py (linux_root_only) confirms the same behavior
for real, as real root, on colima -- not just mocked."""
from __future__ import annotations

import os

import pytest

from siphonophore_core.execution import ExecutionError, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy


def _make_intent(**overrides) -> Intent:
    defaults = dict(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low", artifact_code="pass")
    defaults.update(overrides)
    return Intent(**defaults)


@pytest.fixture
def gate() -> Gate:
    return Gate(ConsequencePolicy())


@pytest.fixture
def fake_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)


@pytest.fixture
def fake_non_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)


def test_same_process_refuses_when_euid_is_root(gate: Gate, fake_root):
    backend = SameProcessBackend()
    intent = _make_intent()
    decision = gate.submit(intent)
    with pytest.raises(ExecutionError, match="euid 0"):
        backend.run(decision, intent)


def test_separate_process_refuses_when_euid_is_root(gate: Gate, fake_root):
    backend = SeparateProcessBackend()
    intent = _make_intent(consequence="high")
    decision = gate.submit(intent)
    with pytest.raises(ExecutionError, match="euid 0"):
        backend.run(decision, intent)


def test_same_process_runs_normally_when_not_root(gate: Gate, fake_non_root):
    backend = SameProcessBackend()
    intent = _make_intent(artifact_code="RESULT = 1")
    decision = gate.submit(intent)
    effect = backend.run(decision, intent)
    assert effect.execution_class == "same_process"


def test_same_process_allow_root_true_bypasses_the_refusal(gate: Gate, fake_root):
    backend = SameProcessBackend(allow_root=True)
    intent = _make_intent(artifact_code="RESULT = 1")
    decision = gate.submit(intent)
    effect = backend.run(decision, intent)  # does not raise
    assert effect.execution_class == "same_process"


def test_separate_process_allow_root_true_bypasses_the_refusal(gate: Gate, fake_root):
    backend = SeparateProcessBackend(allow_root=True)
    intent = _make_intent(consequence="high", artifact_code="pass")
    decision = gate.submit(intent)
    effect = backend.run(decision, intent)  # does not raise -- a real subprocess actually runs
    assert effect.execution_class == "separate_process"


def test_refusal_is_checked_before_artifact_code_presence(gate: Gate, fake_root):
    """The root check should fail first and clearly, not get masked by a different, less specific
    error (e.g. "requires intent.artifact_code") when both conditions are true at once."""
    intent = Intent(kind="run_artifact", principal_id="alice", intent_id="i-1", consequence="low")  # no artifact_code
    decision = gate.submit(intent)
    backend = SameProcessBackend()
    with pytest.raises(ExecutionError, match="euid 0"):
        backend.run(decision, intent)
