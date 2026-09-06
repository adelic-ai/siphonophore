"""Parses a Model's raw completion text into a ParsedTurn: an optional human-facing `message` and
an optional `Intent`, the only thing that ever reaches the Gate.

`parse_turn()` is the envelope-level parser -- it owns fence-stripping, JSON decoding, and the
top-level `{"message", "operation"}` schema. `"operation"` is the only thing a completion can name
that becomes real-world effect; its absence means no Intent is constructed at all, no matter what
`"message"` says. `parse_intent()` is narrower and unchanged in spirit from before this file's
turn-contract update: it is the ONLY place an `operation` object becomes an Intent, and it produces
nothing but an Intent -- never a Decision, never a reference to Gate or Executor internals. A
model's output has no way to name a Decision's token, because a Decision does not exist yet at this
point in the pipeline; the Gate is the only thing that can ever mint one (mediation.py), from its
own secret the model has no access to. This is what makes it structurally impossible for a
completion, however adversarial, to skip the Gate: there is no field in Intent's schema for a
pre-authorized Decision, and nothing downstream of parse_intent() accepts an Effect from anywhere
but Broker.dispatch() -> Gate.submit() -> Executor.execute() (broker.py).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from siphonophore_core.intent import Intent

TOP_LEVEL_ALLOWED_FIELDS = {"message", "operation"}
OPERATION_REQUIRED_FIELDS = ("kind", "artifact_code")
OPERATION_ALLOWED_FIELDS = {"kind", "payload", "consequence", "artifact_code"}


@dataclass(frozen=True)
class ParsedTurn:
    """What one completion actually contains: an optional human-facing `message` and an optional
    `Intent` (the only thing that ever reaches the Gate). `intent is None` means no operation was
    requested this turn -- not a manufactured no-op Intent, not an Intent with a null
    `artifact_code`, structurally nothing at all. `message` is pure display text -- it is never
    passed to Broker.dispatch(), never reaches Gate or Executor, and has no effect on what gets
    authorized or how. Splitting it out here rather than folding it into Intent keeps
    siphonophore-core free of anything conversational (DESIGN.md section 6: no Conversation
    concept in the core) -- this is a harness-only concept, for a harness that chooses to show a
    human what the model said alongside what it did."""

    message: str | None = None
    intent: Intent | None = None


class IntentParseError(ValueError):
    """The completion, or the operation it named, did not describe a well-formed turn. Distinct
    from any Gate/Executor error -- this fails before an Intent object even exists, let alone
    before it reaches the Gate."""


def _strip_code_fence(text: str) -> str:
    """A real model, even when instructed to emit raw JSON, commonly wraps it in a markdown code
    fence (```json ... ``` or ``` ... ```) out of habit. Stripping one exact, well-formed fence is
    a formatting normalization, not a loosening of the schema check below -- json.loads() still
    has to succeed on whatever remains, and anything that isn't a clean, complete fence is left
    untouched and fails json.loads() the same way it always did."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and len(stripped) >= 6:
        first_newline = stripped.find("\n")
        body_start = first_newline + 1 if first_newline != -1 else 3
        return stripped[body_start:-3].strip()
    return text


def parse_turn(completion: str, principal_id: str) -> ParsedTurn:
    """`completion` is expected to be a single JSON object envelope: {"message": "...",
    "operation": {"kind": ..., "payload": {...}, "consequence": "low"|"high"|"privileged",
    "artifact_code": "..."}}. Both `"message"` and `"operation"` are independently optional --
    `{}`, and `{"operation": null}`, both decode to `ParsedTurn(message=None, intent=None)`: a
    degenerate but harmless turn, not a malformed one. `"operation"` present but not itself a
    well-formed operation object (e.g. missing `"kind"`/`"artifact_code"`, or not a JSON object at
    all) fails closed with IntentParseError -- explicitly naming an operation and then failing to
    describe one is never silently treated as "no operation requested".

    Delegates to parse_intent() only when an operation genuinely exists -- never on conversational
    text alone, so `"message"` has no way to become, or influence, an Intent."""
    try:
        data = json.loads(_strip_code_fence(completion))
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"completion is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntentParseError(f"completion must decode to a JSON object, got {type(data).__name__}")

    unknown = set(data) - TOP_LEVEL_ALLOWED_FIELDS
    if unknown:
        raise IntentParseError(f"completion names unknown top-level fields: {sorted(unknown)}")

    message = data.get("message")
    operation = data.get("operation")
    if operation is None:
        return ParsedTurn(message=message, intent=None)
    if not isinstance(operation, dict):
        raise IntentParseError(f"operation must decode to a JSON object, got {type(operation).__name__}")

    return ParsedTurn(message=message, intent=parse_intent(operation, principal_id))


def parse_intent(operation: dict, principal_id: str) -> Intent:
    """`operation` is an already-JSON-decoded object naming the one requested effect:
    {"kind": ..., "payload": {...}, "consequence": "low"|"high"|"privileged", "artifact_code":
    "..."}. Called only by parse_turn(), and only when an operation genuinely exists -- never on
    raw completion text, and never on conversational text.

    `artifact_code` is required, non-null, and non-empty: `SameProcessBackend`/
    `SeparateProcessBackend` (the only backends `portable_profile()` registers, composition.py)
    both unconditionally require it (execution.py), so an Intent this parser hands to Broker
    without one would be authorized only to fail at the backend. Rejecting it here, before an
    Intent even exists, is the earliest point that can be checked without touching
    siphonophore_core -- the backend's own check (execution.py) stays exactly as it is, as defense
    in depth for any other caller that constructs an Intent by hand.

    `intent_id` is always freshly generated here, never taken from the completion -- the model has
    no legitimate reason to name its own intent_id, and accepting one from untrusted text would let
    a completion claim to be a replay of, or collide with, an intent_id the Gate has already minted
    a Decision for."""
    unknown = set(operation) - OPERATION_ALLOWED_FIELDS
    if unknown:
        raise IntentParseError(f"operation names unknown fields: {sorted(unknown)}")
    if "kind" not in operation:
        raise IntentParseError("operation is missing required field: 'kind'")
    if not operation.get("artifact_code"):
        raise IntentParseError("operation is missing required field: 'artifact_code'")

    return Intent(
        kind=operation["kind"],
        principal_id=principal_id,
        intent_id=str(uuid.uuid4()),
        payload=operation.get("payload", {}),
        consequence=operation.get("consequence", "low"),
        artifact_code=operation.get("artifact_code"),
    )
