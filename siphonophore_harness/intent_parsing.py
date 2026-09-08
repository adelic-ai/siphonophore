"""Parses a Model's raw completion text into a ParsedTurn: an optional human-facing `message`, an
optional `Intent` (the only thing that ever reaches the Gate), and an optional `WorkOrder`
(never reaches the Gate at all -- see `work_order.py`).

`parse_turn()` is the envelope-level parser -- it owns fence-stripping, JSON decoding, and the
top-level `{"message", "operation", "work_order"}` schema. `"operation"` is the only thing a
completion can name that becomes real-world effect; its absence means no Intent is constructed at
all, no matter what `"message"` says. A completion may not name both `"operation"` and
`"work_order"` in the same turn -- compiling a work order is not an operation, and mixing the two
would blur a distinction this harness otherwise keeps sharp. `parse_intent()` is narrower: it is
the ONLY place an `operation` object becomes an Intent, and it produces nothing but an Intent --
never a Decision, never a reference to Gate or Executor internals. A model's output has no way to
name a Decision's token, because a Decision does not exist yet at this point in the pipeline; the
Gate is the only thing that can ever mint one (mediation.py), from its own secret the model has no
access to. This is what makes it structurally impossible for a completion, however adversarial, to
skip the Gate: there is no field in Intent's schema for a pre-authorized Decision, and nothing
downstream of parse_intent() accepts an Effect from anywhere but
Broker.dispatch() -> Gate.submit() -> Executor.execute() (broker.py).

`artifact_code` is required, non-null, non-empty for CODE_BEARING_KINDS (kinds whose entire
behavior IS the Python code -- `run_artifact`, `write_file`) and forbidden for every other kind
(docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): a typed operation like `read_file`/`list_directory`/
`search_repository` has fixed, non-code behavior, and a model attaching `artifact_code` to one
would be attaching code that is silently never read by any backend -- forbidding it outright is
cheap and prevents that confusing, misleading shape from ever reaching a backend at all. This
membership test is a fixed harness-level policy, not configured per active profile: an active
profile that never allows `run_artifact`/`write_file` at the Gate/Policy level (e.g. the reference
"planning" profile, composition.py) makes this distinction moot for its own sessions rather than
needing to reconfigure it away.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from siphonophore_core.intent import Intent

from .work_order import WorkOrder, WorkOrderParseError, parse_work_order

TOP_LEVEL_ALLOWED_FIELDS = {"message", "operation", "work_order"}
OPERATION_REQUIRED_FIELDS = ("kind",)
OPERATION_ALLOWED_FIELDS = {"kind", "payload", "consequence", "artifact_code"}

# Kinds whose entire behavior IS the attached Python source -- every other kind is a typed
# operation whose behavior is fixed by its backend (execution_readonly.py and friends), never by
# caller-supplied code.
CODE_BEARING_KINDS = frozenset({"run_artifact", "write_file"})


@dataclass(frozen=True)
class ParsedTurn:
    """What one completion actually contains: an optional human-facing `message`, an optional
    `Intent` (the only thing that ever reaches the Gate), and an optional `WorkOrder` (never
    reaches the Gate, never becomes an Intent). At most one of `intent`/`work_order` is ever
    non-None -- `parse_turn()` refuses a completion naming both.

    `intent is None` means no operation was requested this turn -- not a manufactured no-op
    Intent, not an Intent with a null `artifact_code`, structurally nothing at all. `message` is
    pure display text -- it is never passed to Broker.dispatch(), never reaches Gate or Executor,
    and has no effect on what gets authorized or how. Splitting these out here rather than folding
    them into Intent keeps siphonophore-core free of anything conversational (DESIGN.md section 6:
    no Conversation concept in the core) -- this is a harness-only concept, for a harness that
    chooses to show a human what the model said alongside what it did."""

    message: str | None = None
    intent: Intent | None = None
    work_order: WorkOrder | None = None


class IntentParseError(ValueError):
    """The completion, or the operation/work_order it named, did not describe a well-formed turn.
    Distinct from any Gate/Executor error -- this fails before an Intent object even exists, let
    alone before it reaches the Gate."""


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
    "artifact_code": "..."}, "work_order": {...}}. All three top-level keys are independently
    optional, EXCEPT that `"operation"` and `"work_order"` may not both be present in the same
    completion. `{}`, and `{"operation": null}`/`{"work_order": null}`, all decode to a degenerate
    but harmless turn, not a malformed one. `"operation"`/`"work_order"` present but not itself
    well-formed (e.g. missing a required field, or not a JSON object at all) fails closed with
    IntentParseError -- explicitly naming one and then failing to describe it is never silently
    treated as "nothing requested".

    Delegates to parse_intent()/parse_work_order() only when the respective field genuinely
    exists -- never on conversational text alone, so `"message"` has no way to become, or
    influence, an Intent or a WorkOrder."""
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
    work_order_data = data.get("work_order")
    if operation is not None and work_order_data is not None:
        raise IntentParseError("completion names both 'operation' and 'work_order' -- only one is allowed per turn")

    intent = None
    if operation is not None:
        if not isinstance(operation, dict):
            raise IntentParseError(f"operation must decode to a JSON object, got {type(operation).__name__}")
        intent = parse_intent(operation, principal_id)

    work_order = None
    if work_order_data is not None:
        try:
            work_order = parse_work_order(work_order_data, work_order_id=str(uuid.uuid4()))
        except WorkOrderParseError as exc:
            raise IntentParseError(str(exc)) from exc

    return ParsedTurn(message=message, intent=intent, work_order=work_order)


def parse_intent(operation: dict, principal_id: str) -> Intent:
    """`operation` is an already-JSON-decoded object naming the one requested effect:
    {"kind": ..., "payload": {...}, "consequence": "low"|"high"|"privileged", "artifact_code":
    "..."}. Called only by parse_turn(), and only when an operation genuinely exists -- never on
    raw completion text, and never on conversational text.

    `artifact_code`: required, non-null, non-empty for CODE_BEARING_KINDS (`run_artifact`,
    `write_file`) -- `SameProcessBackend`/`SeparateProcessBackend` unconditionally require it
    (execution.py), so an Intent this parser hands to Broker without one would be authorized only
    to fail at the backend; rejecting it here is the earliest point that can be checked without
    touching siphonophore_core, and the backend's own check stays exactly as it is, as defense in
    depth for any other caller that constructs an Intent by hand. FORBIDDEN for every other kind --
    a typed operation's behavior is fixed by its backend, never by caller-supplied code, so
    attaching code there would be silently ignored and misleading.

    `intent_id` is always freshly generated here, never taken from the completion -- the model has
    no legitimate reason to name its own intent_id, and accepting one from untrusted text would let
    a completion claim to be a replay of, or collide with, an intent_id the Gate has already minted
    a Decision for."""
    unknown = set(operation) - OPERATION_ALLOWED_FIELDS
    if unknown:
        raise IntentParseError(f"operation names unknown fields: {sorted(unknown)}")
    if "kind" not in operation:
        raise IntentParseError("operation is missing required field: 'kind'")
    payload = operation.get("payload", {})
    if not isinstance(payload, dict):
        raise IntentParseError(f"operation 'payload' must decode to a JSON object, got {type(payload).__name__}")
    kind = operation["kind"]
    is_code_bearing = kind in CODE_BEARING_KINDS
    if is_code_bearing and not operation.get("artifact_code"):
        raise IntentParseError(f"operation kind {kind!r} is missing required field: 'artifact_code'")
    # `is not None`, not truthiness: an empty string is a legitimate (if useless) value for the
    # REQUIRED case above to reject via `not operation.get(...)`, but for the FORBIDDEN case here
    # it must not be allowed to slip through merely because "" is falsy -- a non-code-bearing kind
    # (e.g. read_file) explicitly naming artifact_code="" is exactly as forbidden as naming real
    # code, per this function's own docstring.
    if not is_code_bearing and operation.get("artifact_code") is not None:
        raise IntentParseError(f"operation kind {kind!r} must not include 'artifact_code' -- it is not code-bearing")

    return Intent(
        kind=kind,
        principal_id=principal_id,
        intent_id=str(uuid.uuid4()),
        payload=payload,
        consequence=operation.get("consequence", "low"),
        artifact_code=operation.get("artifact_code"),
    )
