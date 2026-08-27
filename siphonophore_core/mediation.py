"""Gate -- the one mediation boundary every effect-producing Intent must pass through
(DESIGN.md section 1).

The only thing that mints a valid Decision, Order, or Authority. `Executor` (execution.py)
independently verifies every Decision it's given -- never trusting one merely because it was
handed one; `Gate` itself follows the identical discipline internally when minting a delegated
Authority (see `delegate()`) -- it never trusts a parent Authority because some caller already
checked it, it re-verifies that parent itself, every time.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

from .authority import Authority, Order, Scope
from .intent import Intent
from .policy import Decision, Policy


def digest_of(code: str) -> str:
    """sha256 hex digest of an artifact's code -- DESIGN.md section 8's execution provenance.
    A module-level function, not a Gate method, so `Executor` can independently recompute it at
    execution time without needing a reference to the Gate that minted the original Decision --
    the whole point of the check (lab/008's `predicate_b`) is that this recomputation happens
    somewhere the original computation can't influence."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class Gate:
    """Mints and verifies Decisions, Orders, and Authorities. `_secret` never leaves this instance
    -- callers get a bound `verify`/`verify_authority`/`verify_order` capability (this instance
    itself, treated as one), never the secret.

    Binds every dispatch-relevant field into the Decision HMAC: `kind`, `permitted`,
    `execution_class`, `artifact_digest`, `authority_id`, `order_id`. This is a standing rule, not
    a style choice -- discovered twice independently while building the lab experiments (lab/002
    found it for `kind`, lab/003 found it again for `execution_class`, having to relearn the same
    lesson because it hadn't yet been generalized). Any field added to `Decision` in the future
    that `Executor.execute()` branches on must be added to `_canonical`/`_mint` in the same change
    that adds the field, not after. `Authority`/`Order` get the identical treatment via their own
    canonical/mint pair below -- same secret, same discipline, deliberately not a different
    mechanism."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._secret = secrets.token_bytes(32)

    # ---- Decision --------------------------------------------------------------------------

    def _canonical(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str,
        artifact_digest: str, authority_id: str, order_id: str,
    ) -> bytes:
        return (
            f"{intent_id}:{principal_id}:{kind}:{permitted}:{execution_class}:{artifact_digest}:"
            f"{authority_id}:{order_id}"
        ).encode("utf-8")

    def _mint(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str,
        artifact_digest: str, authority_id: str, order_id: str,
    ) -> str:
        msg = self._canonical(
            intent_id, principal_id, kind, permitted, execution_class, artifact_digest, authority_id, order_id
        )
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent, authority: Authority | None = None) -> Decision:
        """Mints a Decision for `intent`. `authority` is optional and, when omitted, behaves
        exactly as before this parameter existed -- an authority-less submission, evaluated purely
        by `self._policy`, `Decision.authority_id`/`order_id` both `None`.

        When `authority` is given, three checks run before policy is even consulted, each
        independent of whatever the caller may have already checked:
        1. `authority` itself is independently re-verified (`self.verify_authority`) -- Gate never
           trusts that Executor, or anything else, already confirmed it.
        2. `intent.principal_id == authority.principal_id` -- a real, Gate-verified Authority
           object is a bearer capability; without this check, one principal's leaked/observed
           Authority could be used to submit on a different principal's behalf.
        3. `intent.kind in authority.scope.allowed_kinds` -- the exercised authority must actually
           cover what's being attempted (property: "the effect requested is within the delegated
           authority").
        A mismatch in (1) or (2) is a malformed/forged request, not a policy question -- raises
        GateViolation immediately, mints nothing, exactly how a decision/intent identity mismatch
        is already handled below. A miss in (3) is folded into `permitted` alongside the ordinary
        policy result -- a real, signed, auditable "no," the same shape as any other policy
        denial, not an exception."""
        authority_id = ""
        order_id = ""
        if authority is not None:
            if not self.verify_authority(authority):
                raise GateViolation("authority failed Gate verification -- forged, tampered, or unknown")
            if intent.principal_id != authority.principal_id:
                raise GateViolation("intent principal does not match the authority being exercised")
            authority_id = authority.authority_id
            order_id = authority.order_id

        permitted, execution_class = self._policy.evaluate(intent)
        if authority is not None and intent.kind not in authority.scope.allowed_kinds:
            permitted = False
        artifact_digest = digest_of(intent.artifact_code) if intent.artifact_code is not None else ""
        token = self._mint(
            intent.intent_id, intent.principal_id, intent.kind, permitted, execution_class,
            artifact_digest, authority_id, order_id,
        )
        return Decision(
            intent_id=intent.intent_id,
            principal_id=intent.principal_id,
            kind=intent.kind,
            permitted=permitted,
            execution_class=execution_class,
            artifact_digest=artifact_digest,
            token=token,
            authority_id=authority_id or None,
            order_id=order_id or None,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id,
            decision.principal_id,
            decision.kind,
            decision.permitted,
            decision.execution_class,
            decision.artifact_digest,
            decision.authority_id or "",
            decision.order_id or "",
        )
        return hmac.compare_digest(expected, decision.token)

    # ---- Order -------------------------------------------------------------------------------

    def _order_canonical(self, order_id: str, issuer: str, granted_kinds: frozenset[str], max_delegation_depth: int) -> bytes:
        kinds = ",".join(sorted(granted_kinds))
        return f"{order_id}:{issuer}:{kinds}:{max_delegation_depth}".encode("utf-8")

    def _mint_order(self, order_id: str, issuer: str, granted_kinds: frozenset[str], max_delegation_depth: int) -> str:
        msg = self._order_canonical(order_id, issuer, granted_kinds, max_delegation_depth)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def issue_order(self, order_id: str, issuer: str, granted_kinds: frozenset[str], max_delegation_depth: int) -> Order:
        """Mints an Order -- the ungrounded root of an authority chain. Not derived from anything
        else within this model; `issuer` is asserted, not independently authenticated, exactly the
        same disclosed-limitation shape as `Intent.consequence` already is (see policy.py)."""
        granted = frozenset(granted_kinds)
        token = self._mint_order(order_id, issuer, granted, max_delegation_depth)
        return Order(order_id=order_id, issuer=issuer, granted_kinds=granted, max_delegation_depth=max_delegation_depth, token=token)

    def verify_order(self, order: Order) -> bool:
        expected = self._mint_order(order.order_id, order.issuer, order.granted_kinds, order.max_delegation_depth)
        return hmac.compare_digest(expected, order.token)

    # ---- Authority -----------------------------------------------------------------------------

    def _authority_canonical(
        self, authority_id: str, principal_id: str, order_id: str, parent_authority_id: str,
        allowed_kinds: frozenset[str], remaining_delegation_depth: int,
    ) -> bytes:
        kinds = ",".join(sorted(allowed_kinds))
        return (
            f"{authority_id}:{principal_id}:{order_id}:{parent_authority_id}:{kinds}:{remaining_delegation_depth}"
        ).encode("utf-8")

    def _mint_authority(
        self, authority_id: str, principal_id: str, order_id: str, parent_authority_id: str | None,
        allowed_kinds: frozenset[str], remaining_delegation_depth: int,
    ) -> str:
        msg = self._authority_canonical(
            authority_id, principal_id, order_id, parent_authority_id or "", allowed_kinds, remaining_delegation_depth
        )
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def grant_root_authority(self, order: Order, principal_id: str, allowed_kinds: frozenset[str] | None = None) -> Authority:
        """Derives a principal's first Authority directly from a verified Order -- independently
        re-verified here (`self.verify_order`), not trusted because a caller holds an Order object
        that merely looks right. `allowed_kinds`, if given, must be a subset of what the Order
        itself grants -- a root grant can narrow an Order's authority, never broaden it."""
        if not self.verify_order(order):
            raise GateViolation("order failed Gate verification -- forged, tampered, or unknown")
        kinds = frozenset(allowed_kinds) if allowed_kinds is not None else order.granted_kinds
        if not kinds.issubset(order.granted_kinds):
            raise GateViolation("requested authority exceeds what the order grants")
        authority_id = uuid.uuid4().hex
        scope = Scope(allowed_kinds=kinds, remaining_delegation_depth=order.max_delegation_depth)
        token = self._mint_authority(authority_id, principal_id, order.order_id, None, scope.allowed_kinds, scope.remaining_delegation_depth)
        return Authority(
            authority_id=authority_id, principal_id=principal_id, order_id=order.order_id,
            parent_authority_id=None, scope=scope, token=token,
        )

    def verify_authority(self, authority: Authority) -> bool:
        expected = self._mint_authority(
            authority.authority_id, authority.principal_id, authority.order_id, authority.parent_authority_id,
            authority.scope.allowed_kinds, authority.scope.remaining_delegation_depth,
        )
        return hmac.compare_digest(expected, authority.token)

    def delegate(self, parent_authority: Authority, to_principal_id: str, allowed_kinds: frozenset[str] | None = None) -> Authority:
        """Derives a narrower Authority for `to_principal_id` from `parent_authority`.

        The exact guarantee this produces, stated precisely rather than oversold: minting only
        proceeds after Gate has independently re-verified `parent_authority` itself (not trusted
        because some caller already checked it) and confirmed the requested scope is a subset of
        the parent's own scope with delegation depth remaining. The resulting Authority's
        `order_id`/`parent_authority_id` are Gate's attestation, made at THIS minting, that those
        derivation rules held against the parent it verified -- not the child object independently
        reconstructing or re-proving the entire ancestry chain on its own. That stronger property
        would need each hop to be checkable without trusting Gate's own minting discipline (e.g.
        independent per-link signatures); this system has exactly one Gate mediating every hop, so
        by induction the chain is sound as long as Gate's own re-verify-before-mint discipline
        holds at every step -- which is a real, meaningful guarantee, just not the same claim as
        "self-proving without Gate." Verify with `verify_authority()`, never by re-deriving the
        chain from the object's fields alone."""
        if not self.verify_authority(parent_authority):
            raise GateViolation("parent authority failed Gate verification -- forged, tampered, or unknown")
        if parent_authority.scope.remaining_delegation_depth <= 0:
            raise GateViolation("parent authority has no remaining delegation depth")
        kinds = frozenset(allowed_kinds) if allowed_kinds is not None else parent_authority.scope.allowed_kinds
        if not kinds.issubset(parent_authority.scope.allowed_kinds):
            raise GateViolation("requested delegated authority exceeds the parent authority's own scope")

        authority_id = uuid.uuid4().hex
        scope = Scope(allowed_kinds=kinds, remaining_delegation_depth=parent_authority.scope.remaining_delegation_depth - 1)
        token = self._mint_authority(
            authority_id, to_principal_id, parent_authority.order_id, parent_authority.authority_id,
            scope.allowed_kinds, scope.remaining_delegation_depth,
        )
        return Authority(
            authority_id=authority_id, principal_id=to_principal_id, order_id=parent_authority.order_id,
            parent_authority_id=parent_authority.authority_id, scope=scope, token=token,
        )


class GateViolation(PermissionError):
    """A Decision failed verification, was denied by policy, or does not correspond to the Intent
    it's being used with. Base class for more specific violations raised elsewhere (e.g.
    execution.py's ArtifactMismatchError)."""
