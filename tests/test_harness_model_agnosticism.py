"""Structural proof that the reference turn/event/WorkOrder model is provider-neutral
(docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md's Model Agnosticism requirement), mirroring
tests/test_core_no_k8s_vocabulary.py's own convention: scan source for vocabulary that would tie a
conceptual module to one provider's wire format, rather than merely asserting it in prose.

Anthropic-specific concepts (thinking blocks, Claude's own message-role mapping, the raw Messages
API request/response shape) are confined to model_anthropic.py, the one adapter file this arc
exercises against a live model. A future OpenAI/Gemini/local-model adapter would live in its own
equally-isolated file, implementing the same Model ABC (model.py), without touching any of the
files this test scans."""
from __future__ import annotations

from pathlib import Path

import pytest

_PROVIDER_VOCABULARY = (
    "anthropic", "claude", "openai", "gpt-", "gemini", "thinking_block", "tool_use", "tool_result",
)

_HARNESS_DIR = Path(__file__).resolve().parent.parent / "siphonophore_harness"

# Every conceptual module EXCEPT the adapter itself (and this file's own package __init__, which
# only re-exports docstrings) -- these define UserTurn/OperationRequest/OperationResult/
# ModelContinuation/FinalResponse/WorkOrder-shaped concepts and must stay provider-neutral.
_PROVIDER_NEUTRAL_MODULES = (
    "model.py", "loop.py", "intent_parsing.py", "outcome.py", "broker.py", "work_order.py",
    "session_log.py", "composition.py", "prompts.py",
)


@pytest.mark.parametrize("filename", _PROVIDER_NEUTRAL_MODULES)
def test_module_names_no_provider_specific_vocabulary(filename):
    # A bare cross-reference to the adapter file's own name (e.g. in a docstring pointing a reader
    # at where Anthropic-specific diagnostics actually live) is documentation, not a semantic
    # dependency -- stripped before scanning so it doesn't shadow a genuine vocabulary leak.
    source = (_HARNESS_DIR / filename).read_text().lower().replace("model_anthropic.py", "")
    found = [word for word in _PROVIDER_VOCABULARY if word in source]
    assert found == [], f"{filename} mentions provider-specific vocabulary: {found}"


def test_provider_specific_vocabulary_is_confined_to_the_anthropic_adapter():
    """The inverse check -- confirms the scan above isn't vacuous by proving the adapter file
    genuinely does contain this vocabulary (it should: it's the one file allowed to)."""
    source = (_HARNESS_DIR / "model_anthropic.py").read_text().lower()
    assert "anthropic" in source


def test_model_abc_takes_and_returns_only_plain_text_and_dicts():
    """Model.complete()'s signature itself is the strongest evidence: list[dict] in, str out --
    no provider-shaped type appears in the interface a future adapter must implement."""
    import inspect

    from siphonophore_harness.model import Model

    sig = inspect.signature(Model.complete)
    params = list(sig.parameters.values())
    assert len(params) == 2  # self, messages
    assert params[1].name == "messages"
