from __future__ import annotations

from siphonophore_harness.intent_parsing import ALLOWED_FIELDS
from siphonophore_harness.prompts import DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_names_every_schema_field():
    for field in ("kind", "payload", "consequence", "artifact_code", "message"):
        assert f'"{field}"' in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_names_exactly_the_fields_parse_intent_allows():
    """Catches drift in either direction: a field parse_intent accepts but the prompt never
    mentions (the model has no reason to use it), or a field the prompt implies exists but
    parse_intent would reject as unknown."""
    for field in ALLOWED_FIELDS:
        assert f'"{field}"' in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_names_the_real_default_kinds_and_consequences():
    """Must match ConsequencePolicy's actual default vocabulary (policy.py) -- an invented
    vocabulary here would just teach a real model to describe intents the default policy denies."""
    for kind in ("write_file", "run_artifact", "delegate"):
        assert kind in DEFAULT_SYSTEM_PROMPT
    for consequence in ("low", "high", "privileged"):
        assert consequence in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_warns_against_the_fields_parse_intent_rejects():
    assert "intent_id" in DEFAULT_SYSTEM_PROMPT
    assert "token" in DEFAULT_SYSTEM_PROMPT
