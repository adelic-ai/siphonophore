"""Parses a Model's raw completion text into a real siphonophore_core.intent.Intent.

The completion is untrusted: it comes from a language model, which may hallucinate, be prompted
adversarially, or (in a test) be deliberately hostile. parse_intent() is the ONLY place completion
text becomes an Intent, and it produces nothing but an Intent -- never a Decision, never a
reference to Gate or Executor internals. A model's output has no way to name a Decision's token,
because a Decision does not exist yet at this point in the pipeline; the Gate is the only thing
that can ever mint one (mediation.py), from its own secret the model has no access to. This is
what makes it structurally impossible for a completion, however adversarial, to skip the Gate:
there is no field in Intent's schema for a pre-authorized Decision, and nothing downstream of
parse_intent() accepts an Effect from anywhere but Broker.dispatch() -> Gate.submit() ->
Executor.execute() (broker.py).
"""
from __future__ import annotations

import json
import uuid

from siphonophore_core.intent import Intent

REQUIRED_FIELDS = ("kind",)
ALLOWED_FIELDS = {"kind", "payload", "consequence", "artifact_code"}


class IntentParseError(ValueError):
    """The completion did not describe a well-formed intent. Distinct from any Gate/Executor
    error -- this fails before an Intent object even exists, let alone before it reaches the
    Gate."""


def parse_intent(completion: str, principal_id: str) -> Intent:
    """`completion` is expected to be a single JSON object naming the intent the model wants to
    make: {"kind": ..., "payload": {...}, "consequence": "low"|"high"|"privileged",
    "artifact_code": "..."}. `intent_id` is always freshly generated here, never taken from the
    completion -- the model has no legitimate reason to name its own intent_id, and accepting one
    from untrusted text would let a completion claim to be a replay of, or collide with, an
    intent_id the Gate has already minted a Decision for."""
    try:
        data = json.loads(completion)
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"completion is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntentParseError(f"completion must decode to a JSON object, got {type(data).__name__}")

    unknown = set(data) - ALLOWED_FIELDS
    if unknown:
        raise IntentParseError(f"completion names unknown intent fields: {sorted(unknown)}")
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise IntentParseError(f"completion is missing required intent fields: {missing}")

    return Intent(
        kind=data["kind"],
        principal_id=principal_id,
        intent_id=str(uuid.uuid4()),
        payload=data.get("payload", {}),
        consequence=data.get("consequence", "low"),
        artifact_code=data.get("artifact_code"),
    )
