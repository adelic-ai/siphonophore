"""Tests for AnthropicAPIModel's own wiring -- message-role translation, system prompt handling,
text-block extraction, and the missing-API-key refusal -- using a fake client satisfying the same
shape as anthropic.Anthropic, not a real network call. No API key, no cost, no live model needed:
this proves the plumbing is correct so the first real network call (once a key exists) is testing
"does a real model's output survive parse_intent", not "did I wire up the client correctly"."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from siphonophore_harness.model_anthropic import AnthropicAPIModel, ModelResponseDiagnostics


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeNonTextBlock:
    """Stands in for any Anthropic content block that isn't type=="text" (e.g. "thinking") --
    deliberately has no `.text` attribute at all, since complete() must never read one from a
    non-text block."""

    type: str


class _FakeMessages:
    def __init__(self, captured: dict, reply_text: str) -> None:
        self._captured = captured
        self._reply_text = reply_text

    def create(self, **kwargs):
        self._captured.update(kwargs)
        return type("FakeMessage", (), {"content": [_FakeTextBlock(text=self._reply_text)]})()


class _FakeAnthropicClient:
    def __init__(self, captured: dict, reply_text: str = "fake completion") -> None:
        self.messages = _FakeMessages(captured, reply_text)


class _FakeBlockMessages:
    """Returns an exact, caller-provided list of content blocks -- unlike _FakeMessages, not
    limited to a single synthesized text block -- so tests can construct any block-type shape
    (text-only, non-text-only, mixed, multiple text blocks)."""

    def __init__(self, blocks: list) -> None:
        self._blocks = blocks

    def create(self, **kwargs):
        return type("FakeMessage", (), {"content": self._blocks})()


class _FakeBlockClient:
    def __init__(self, blocks: list) -> None:
        self.messages = _FakeBlockMessages(blocks)


def _model_with_blocks(blocks: list) -> AnthropicAPIModel:
    model = AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real")
    model._client = _FakeBlockClient(blocks)
    return model


def _model_with_fake_client(monkeypatch, reply_text: str = "fake completion", **kwargs) -> tuple[AnthropicAPIModel, dict]:
    captured: dict = {}
    model = AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real", **kwargs)
    model._client = _FakeAnthropicClient(captured, reply_text=reply_text)  # swap in the fake post-construction
    return model, captured


def test_missing_api_key_refuses_construction(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicAPIModel(model="claude-fake")


def test_explicit_api_key_does_not_need_env_var(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real")  # does not raise


def test_env_var_api_key_is_used_when_not_passed_explicitly(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-from-env")
    AnthropicAPIModel(model="claude-fake")  # does not raise


def test_complete_extracts_text_from_response_content(monkeypatch):
    model, captured = _model_with_fake_client(monkeypatch, reply_text="the real completion text")
    result = model.complete([{"role": "user", "content": "hello"}])
    assert result == "the real completion text"


def test_complete_translates_user_and_assistant_roles(monkeypatch):
    model, captured = _model_with_fake_client(monkeypatch)
    model.complete([
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ])
    assert captured["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]


def test_complete_maps_effect_role_to_user():
    """loop.py's third history role, "effect", has no Anthropic equivalent -- treated as a "user"
    turn (the next thing reported to the model), documented as a known alternation limitation in
    this module's own docstring."""
    captured: dict = {}
    model = AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real")
    model._client = _FakeAnthropicClient(captured)
    model.complete([{"role": "effect", "content": "intent i-1 executed via same_process: {}"}])
    assert captured["messages"] == [{"role": "user", "content": "intent i-1 executed via same_process: {}"}]


def test_complete_passes_max_tokens_and_model():
    captured: dict = {}
    model = AnthropicAPIModel(model="claude-fake-model-id", api_key="sk-fake-not-real", max_tokens=123)
    model._client = _FakeAnthropicClient(captured)
    model.complete([{"role": "user", "content": "hi"}])
    assert captured["model"] == "claude-fake-model-id"
    assert captured["max_tokens"] == 123


def test_complete_omits_system_kwarg_when_not_set():
    captured: dict = {}
    model = AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real")
    model._client = _FakeAnthropicClient(captured)
    model.complete([{"role": "user", "content": "hi"}])
    assert "system" not in captured


def test_complete_passes_system_kwarg_when_set():
    captured: dict = {}
    model = AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real", system="be terse")
    model._client = _FakeAnthropicClient(captured)
    model.complete([{"role": "user", "content": "hi"}])
    assert captured["system"] == "be terse"


# ---- model-boundary diagnostics: block types captured before text-block filtering ----------------
# All four cases from the model-boundary-observability stage: a single text block, text alongside
# a non-text block, non-text-only, and multiple text blocks -- proving diagnostics are recorded
# from response.content before filtering, without changing what complete() itself returns.

def test_last_diagnostics_is_none_before_first_call():
    model = AnthropicAPIModel(model="claude-fake", api_key="sk-fake-not-real")
    assert model.last_diagnostics is None


def test_diagnostics_capture_a_single_text_block():
    model = _model_with_blocks([_FakeTextBlock(text="hello")])
    completion = model.complete([{"role": "user", "content": "hi"}])
    assert completion == "hello"
    diagnostics = model.last_diagnostics
    assert isinstance(diagnostics, ModelResponseDiagnostics)
    assert diagnostics.block_types == ("text",)
    assert diagnostics.text_block_count == 1
    assert diagnostics.total_block_count == 1
    assert diagnostics.retained_text_length == len("hello")


def test_diagnostics_capture_text_alongside_a_non_text_block():
    model = _model_with_blocks([_FakeNonTextBlock(type="thinking"), _FakeTextBlock(text="answer")])
    completion = model.complete([{"role": "user", "content": "hi"}])
    assert completion == "answer"  # non-text blocks still excluded from what the parser receives
    diagnostics = model.last_diagnostics
    assert diagnostics.block_types == ("thinking", "text")  # every block type represented
    assert diagnostics.text_block_count == 1
    assert diagnostics.total_block_count == 2
    assert diagnostics.retained_text_length == len("answer")


def test_diagnostics_capture_non_text_only_response():
    model = _model_with_blocks([_FakeNonTextBlock(type="thinking")])
    completion = model.complete([{"role": "user", "content": "hi"}])
    assert completion == ""
    diagnostics = model.last_diagnostics
    assert diagnostics.block_types == ("thinking",)
    assert diagnostics.text_block_count == 0
    assert diagnostics.total_block_count == 1
    assert diagnostics.retained_text_length == 0


def test_diagnostics_multiple_text_blocks_join_exactly_as_before():
    model = _model_with_blocks([_FakeTextBlock(text="a"), _FakeTextBlock(text="b")])
    completion = model.complete([{"role": "user", "content": "hi"}])
    assert completion == "ab"
    diagnostics = model.last_diagnostics
    assert diagnostics.block_types == ("text", "text")
    assert diagnostics.text_block_count == 2
    assert diagnostics.retained_text_length == len("ab")


def test_diagnostics_do_not_expose_api_key_or_full_response_object():
    model = _model_with_blocks([_FakeTextBlock(text="x")])
    model.complete([{"role": "user", "content": "hi"}])
    diagnostics = model.last_diagnostics
    assert "sk-fake-not-real" not in repr(diagnostics)
    assert not hasattr(diagnostics, "response")
    assert not hasattr(diagnostics, "api_key")
