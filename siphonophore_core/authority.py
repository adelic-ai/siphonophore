"""Order, Authority, Scope -- the minimal, durable representation of delegated authority
(DESIGN.md's Order/Authority/Scope section).

These are deliberately NOT Intent/Decision. An Intent is an attempted exercise of authority; an
Order and an Authority are what makes exercising authority meaningful in the first place -- the
grant, not the attempt. Conflating the two (binding delegation lineage onto Decision/Intent, or
using execution_class as a stand-in for how much authority a principal holds) was tried and
rejected during design -- see DESIGN.md and HISTORY.md for why.

Minted and verified only by Gate (mediation.py), the same secret and the same
mint-then-independently-reverify discipline already proven for Decision. Nothing here is a
persisted/looked-up object -- Gate never stores a Decision or an Authority anywhere. The guarantee
an Authority carries is intentionally narrow, stated precisely rather than oversold: see
Gate.delegate()'s docstring.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    """What an Authority actually permits, and how much of it may be delegated further.

    Deliberately minimal: kind-membership and a remaining-delegation-depth budget. No per-payload
    or per-resource constraints (e.g. "may write_file only under /tmp") -- real, disclosed future
    work if it's ever needed, not built speculatively now. No isolation/execution-strength
    dimension at all -- that's ExecutionRequirement's job (Decision.execution_class), which Scope
    must never reference or constrain. Conflating the two was the specific mistake this design
    exists to avoid: isolation strength and delegated authority are different questions, and a
    sub-agent legitimately needing MORE isolation than its delegator for one task is not a scope
    violation."""

    allowed_kinds: frozenset[str]
    remaining_delegation_depth: int


@dataclass(frozen=True)
class Order:
    """The ungrounded root of an authority chain -- the originating authorization/request and its
    issuer. Not an Intent: an Order doesn't attempt an effect, it's the fact that makes attempting
    effects possible at all. `issuer` is a plain string (an operator identity, a ticket reference,
    "system-bootstrap") -- no identity system behind it, deliberately, matching how `principal_id`
    is already a plain string everywhere else in this package."""

    order_id: str
    issuer: str
    granted_kinds: frozenset[str]
    max_delegation_depth: int
    token: str


@dataclass(frozen=True)
class Authority:
    """A standing, principal-scoped capability -- genuinely different in shape and lifetime from a
    Decision. A Decision authorizes one specific Intent, once. An Authority is what a principal
    holds going forward, and may itself become the parent of a further-delegated Authority.

    `order_id` is inherited unchanged from `parent_authority_id`'s own order (or is this
    Authority's own grounding Order, if `parent_authority_id` is None) -- see Gate.delegate()'s
    docstring for exactly what this field does and does not prove."""

    authority_id: str
    principal_id: str
    order_id: str
    parent_authority_id: str | None
    scope: Scope
    token: str
