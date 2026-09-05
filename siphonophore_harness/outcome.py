"""Reusable harness-level dispatch outcome representation
(docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md Stage 2).

`Broker.dispatch()` (broker.py) already always mints a Decision through the Gate before calling
`Executor.execute()` -- on every path: success, refusal, or backend failure (broker.py's own
docstring). This module gives a caller (`CognitiveLoop`, or anyone reaching `Broker` directly) a
curated, non-sensitive way to see what was decided alongside what happened, without exposing the
raw `Decision` or its cryptographic token.

Nothing here changes what Gate/Executor decide or verify. `DecisionProjection` is a read-only view
built from fields `Decision` already exposes (policy.py); `DispatchResult` composes an existing
`Effect` with that view; `classify_outcome()` is a pure function over existing
`siphonophore_core` exception types (plus the three `GateViolation` subclasses `execution.py`
added in Stage 1). None of this is a mandatory layer -- a caller composing `Gate`/`Executor`
directly, bypassing `Broker` entirely, never sees any of it and loses nothing by not using it.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from siphonophore_core.execution import (
    ArtifactMismatchError,
    DecisionVerificationError,
    ExecutionError,
    NoBackendRegisteredError,
    PolicyDeniedError,
)
from siphonophore_core.identity import IdentityError
from siphonophore_core.intent import Effect
from siphonophore_core.mediation import GateViolation
from siphonophore_core.policy import Decision

from .intent_parsing import IntentParseError


@dataclass(frozen=True)
class DecisionProjection:
    """A curated view of the Decision that authorized or refused one dispatch -- deliberately
    excludes `token` (meaningless outside Gate, mediation.py) and `artifact_digest` (already
    implicitly confirmed by a successful execution, and moot on refusal). Built from fields
    `Decision` already exposes (policy.py); this is a view onto an existing capability, not a new
    one -- nothing here lets a holder do anything a bare Effect/exception couldn't already prove
    happened."""

    permitted: bool
    execution_class: str
    authority_id: str | None
    order_id: str | None

    @classmethod
    def from_decision(cls, decision: Decision) -> "DecisionProjection":
        return cls(
            permitted=decision.permitted,
            execution_class=decision.execution_class,
            authority_id=decision.authority_id,
            order_id=decision.order_id,
        )


@dataclass(frozen=True)
class DispatchResult:
    """What `Broker.dispatch()` returns on a successful dispatch: the `Effect` a backend
    produced, plus a `DecisionProjection` of the `Decision` that authorized it.

    Effect-compatible by delegation -- `intent_id`, `execution_class`, and `detail` read straight
    through to the wrapped `Effect` -- so existing code written against a bare `Effect`
    (`CognitiveLoop`'s own `_describe_effect`, `examples/repl.py`'s verbose printing) needs no
    change. This is NOT an `Effect` subclass and does not claim to literally be one: it exposes
    the same three attributes callers already read, nothing more."""

    effect: Effect
    decision: DecisionProjection

    @property
    def intent_id(self) -> str:
        return self.effect.intent_id

    @property
    def execution_class(self) -> str:
        return self.effect.execution_class

    @property
    def detail(self) -> dict:
        return self.effect.detail


class OutcomeCategory(str, Enum):
    """The closed set of outcome categories docs/REFERENCE_HARNESS_TARGET_DESIGN.md section 9
    defines over `Broker.dispatch()`'s possible results. A `str` subclass so a category compares
    equal to, and prints as, its own name -- convenient for a future presentation layer without
    this module needing to know anything about presentation."""

    EXECUTED = "executed"
    INPUT_REJECTED = "input_rejected"
    AUTHORITY_REJECTED = "authority_rejected"
    DENIED = "denied"
    INTEGRITY_REJECTED = "integrity_rejected"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    IDENTITY_REJECTED = "identity_rejected"
    EXECUTION_FAILED = "execution_failed"


def classify_outcome(outcome: DispatchResult | BaseException) -> OutcomeCategory:
    """Pure, deterministic, substrate-neutral: maps a `Broker.dispatch()` outcome (its return
    value on success, or the exception it raised) to exactly one `OutcomeCategory`, using only the
    existing type of `outcome` -- never a message string, a backend-specific `detail` key, or any
    other rendered/human-facing text. Every branch is grounded in an existing, already-tested
    `siphonophore_core` distinction (docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md section 5's
    exception-hierarchy analysis); this function adds no new distinction of its own.

    `authority_rejected` vs. an unclassifiable bare `GateViolation`: both share the identical base
    class (mediation.py's pre-Decision authority/principal-mismatch refusal, and execution.py's
    decision/intent-correspondence site, respectively -- Stage 1 deliberately left both bare; see
    execution.py's `Executor.execute()` and the implementation plan's Refinement 1). They are
    distinguished here not by type (there is only one shared type) but by whether `Broker` (see
    `broker.py`) actually attached a `DecisionProjection` to the exception -- which it only ever
    does after a `Decision` was minted. The decision/intent-correspondence site is unreachable
    through `Broker.dispatch()` at all (`Broker` always executes with the same `Intent` it minted
    the `Decision` from), so this ambiguity is theoretical, not observed through the sanctioned
    path; if it were ever observed, this function refuses to guess rather than mislabeling it.
    """
    if isinstance(outcome, DispatchResult):
        return OutcomeCategory.EXECUTED
    if isinstance(outcome, IntentParseError):
        return OutcomeCategory.INPUT_REJECTED
    if isinstance(outcome, (DecisionVerificationError, ArtifactMismatchError)):
        return OutcomeCategory.INTEGRITY_REJECTED
    if isinstance(outcome, PolicyDeniedError):
        return OutcomeCategory.DENIED
    if isinstance(outcome, NoBackendRegisteredError):
        return OutcomeCategory.BACKEND_UNAVAILABLE
    if isinstance(outcome, IdentityError):
        return OutcomeCategory.IDENTITY_REJECTED
    if isinstance(outcome, ExecutionError):
        return OutcomeCategory.EXECUTION_FAILED
    if isinstance(outcome, GateViolation):
        if getattr(outcome, "decision", None) is None:
            return OutcomeCategory.AUTHORITY_REJECTED
        raise ValueError(
            "unclassifiable GateViolation: a Decision was minted but no specific outcome "
            "subclass applies (decision/intent correspondence failure) -- out of scope per "
            "docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md Refinement 1, unreachable through "
            "Broker.dispatch()'s own path"
        )
    raise ValueError(f"unclassifiable outcome type: {type(outcome).__name__}")
