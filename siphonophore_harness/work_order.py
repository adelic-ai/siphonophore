"""WorkOrder -- the self-contained execution specification a planning session may compile once the
user and model converge (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md). Harness-only concept, the same
reasoning DESIGN.md section 6 already applies to `message`/`ParsedTurn`: a WorkOrder is
conversational/workflow content, never something `siphonophore_core` has any concept of, and never
routed through `Broker.dispatch()` -- compiling one is not an effect, it is a structured
conversational artifact the model proposes.

A WorkOrder GRANTS NOTHING. It is a structured request/recommendation, never a capability: turning
a finalized WorkOrder into real workers with real authority, real credentials, and a real execution
profile is the still-unbuilt Constructor's job (deliberately out of this arc's scope) -- every
"*_requirements" field here is what the workload is asking for, not something this module, or
anything that parses it, hands out. `requested capability != granted authority`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class WorkOrderParseError(ValueError):
    """A `work_order` envelope field did not describe a well-formed WorkOrder. Distinct from
    `intent_parsing.IntentParseError` -- a malformed WorkOrder is not a malformed operation; the
    two concepts never overlap in one completion (`intent_parsing.parse_turn` refuses a completion
    naming both `operation` and `work_order`)."""


_REQUIRED_FIELDS = ("status", "objective", "prompt")
_LIST_FIELDS = (
    "requirements", "constraints", "acceptance_criteria", "context", "prohibited_scope",
    "expected_artifacts", "capability_requirements", "data_requirements", "network_requirements",
    "credential_requirements", "isolation_requirements", "decomposition_suggestions",
)
_SCALAR_OPTIONAL_FIELDS = ("resource_expectations",)
_ALLOWED_FIELDS = set(_REQUIRED_FIELDS) | set(_LIST_FIELDS) | set(_SCALAR_OPTIONAL_FIELDS)
_VALID_STATUSES = frozenset({"draft", "final"})


@dataclass(frozen=True)
class WorkOrder:
    """`work_order_id` is always harness-minted (fresh uuid4 per parsed `work_order` field), never
    accepted from the completion -- the same "a completion has no legitimate reason to name its
    own identifier" principle `intent_parsing.parse_intent` already applies to `intent_id`.

    `status`: `"draft"` (still being iterated on, not yet a finalization signal) or `"final"` (the
    user and model converged; see `is_final`). Neither status touches `siphonophore_core` or
    `Broker.dispatch()` in any way -- both are purely conversational/logging events.

    Every `*_requirements`/`*_scope`/`*_criteria` field is a tuple of plain strings: this class
    makes no attempt to structure or validate their content beyond "a list of strings" -- turning
    prose requirements into a Constructor's actual typed capability grants is explicitly future,
    out-of-scope work."""

    work_order_id: str
    status: str
    objective: str
    prompt: str
    requirements: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    prohibited_scope: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    capability_requirements: tuple[str, ...] = ()
    data_requirements: tuple[str, ...] = ()
    network_requirements: tuple[str, ...] = ()
    credential_requirements: tuple[str, ...] = ()
    isolation_requirements: tuple[str, ...] = ()
    resource_expectations: str | None = None
    decomposition_suggestions: tuple[str, ...] = ()

    @property
    def is_final(self) -> bool:
        return self.status == "final"

    def to_dict(self) -> dict[str, Any]:
        """A plain, JSON-serializable representation -- used by the session event log
        (`session_log.py`) to record a WorkOrder in full, and by any future Constructor input."""
        return {
            "work_order_id": self.work_order_id,
            "status": self.status,
            "objective": self.objective,
            "prompt": self.prompt,
            "requirements": list(self.requirements),
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "context": list(self.context),
            "prohibited_scope": list(self.prohibited_scope),
            "expected_artifacts": list(self.expected_artifacts),
            "capability_requirements": list(self.capability_requirements),
            "data_requirements": list(self.data_requirements),
            "network_requirements": list(self.network_requirements),
            "credential_requirements": list(self.credential_requirements),
            "isolation_requirements": list(self.isolation_requirements),
            "resource_expectations": self.resource_expectations,
            "decomposition_suggestions": list(self.decomposition_suggestions),
        }


def parse_work_order(data: object, *, work_order_id: str) -> WorkOrder:
    """Parses an already-JSON-decoded `work_order` object. Called only by
    `intent_parsing.parse_turn()`, and only when a `work_order` field genuinely exists."""
    if not isinstance(data, dict):
        raise WorkOrderParseError(f"work_order must be a JSON object, got {type(data).__name__}")
    unknown = set(data) - _ALLOWED_FIELDS
    if unknown:
        raise WorkOrderParseError(f"work_order names unknown fields: {sorted(unknown)}")
    missing = [f for f in _REQUIRED_FIELDS if not data.get(f)]
    if missing:
        raise WorkOrderParseError(f"work_order is missing required field(s): {missing}")
    status = data["status"]
    if status not in _VALID_STATUSES:
        raise WorkOrderParseError(f"work_order 'status' must be one of {sorted(_VALID_STATUSES)}, got {status!r}")
    if not isinstance(data["objective"], str) or not isinstance(data["prompt"], str):
        raise WorkOrderParseError("work_order 'objective' and 'prompt' must be strings")

    list_kwargs: dict[str, tuple[str, ...]] = {}
    for field_name in _LIST_FIELDS:
        value = data.get(field_name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WorkOrderParseError(f"work_order field {field_name!r} must be a list of strings")
        list_kwargs[field_name] = tuple(value)

    resource_expectations = data.get("resource_expectations")
    if resource_expectations is not None and not isinstance(resource_expectations, str):
        raise WorkOrderParseError("work_order field 'resource_expectations' must be a string or omitted")

    return WorkOrder(
        work_order_id=work_order_id,
        status=status,
        objective=data["objective"],
        prompt=data["prompt"],
        resource_expectations=resource_expectations,
        **list_kwargs,
    )
