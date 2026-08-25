"""Intent -- what a principal wants to become a real-world effect (DESIGN.md section 1).

An Intent is never executed or trusted directly. It is always submitted to a Gate
(mediation.py), which alone decides whether and how it becomes an Effect.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Intent:
    """A request that something become a real-world effect.

    `intent_id` is caller-supplied, not generated internally -- the shape that survived unchanged
    across lab/004 onward (lab/001-003 had Gate generate it internally; the caller-supplied shape
    won out because a caller submitting an Intent needs to be able to reference its own request
    before submission, e.g. to register a pending check-in keyed by it).

    `consequence` is a disclosed, not-yet-solved simplification, carried forward unchanged from
    every lab experiment: it is a caller-declared field, trusted as-is by the default policy
    (policy.py's `ConsequencePolicy`). DESIGN.md's own "Explicitly open" section names replacing
    this with something independently determined as unresolved -- this package does not pretend to
    have solved it, it gives the caller-declared version a real home instead of leaving it inlined
    nine separate times.

    `artifact_code` is optional -- DESIGN.md section 8's execution provenance (lab/008, lab/009).
    An Intent with no artifact_code produces no digest binding; Gate.submit() reflects that as an
    empty digest, not a fabricated one.
    """

    kind: str
    principal_id: str
    intent_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    consequence: str = "low"
    artifact_code: str | None = None


@dataclass(frozen=True)
class Effect:
    """What an Executor backend reports having done. Still self-report (DESIGN.md section 3) until
    reconciled against independently-observed ground truth (audit.py) -- an Effect is what the
    Executor claims happened, not verified proof that it did."""

    intent_id: str
    execution_class: str
    detail: dict[str, Any] = field(default_factory=dict)
