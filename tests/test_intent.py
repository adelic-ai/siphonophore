from __future__ import annotations

from siphonophore_core.intent import Effect, Intent


def test_intent_defaults():
    intent = Intent(kind="write_file", principal_id="p", intent_id="i")
    assert intent.payload == {}
    assert intent.consequence == "low"
    assert intent.artifact_code is None


def test_intent_is_frozen():
    intent = Intent(kind="write_file", principal_id="p", intent_id="i")
    try:
        intent.kind = "delegate"  # type: ignore[misc]
        assert False, "Intent must be immutable"
    except AttributeError:
        pass


def test_effect_defaults():
    effect = Effect(intent_id="i", execution_class="same_process")
    assert effect.detail == {}
