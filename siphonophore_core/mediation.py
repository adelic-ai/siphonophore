"""Gate -- the one mediation boundary every effect-producing Intent must pass through
(DESIGN.md section 1).

The only thing that mints a valid Decision. `Executor` (execution.py) independently verifies
every Decision it's given -- never trusting one merely because it was handed one.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

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
    """Mints and verifies Decisions. `_secret` never leaves this instance -- callers get a bound
    `verify` capability (this instance itself, treated as one), never the secret.

    Binds every dispatch-relevant field into the HMAC: `kind`, `permitted`, `execution_class`,
    `artifact_digest`. This is a standing rule, not a style choice -- discovered twice
    independently while building the lab experiments (lab/002 found it for `kind`, lab/003 found
    it again for `execution_class`, having to relearn the same lesson because it hadn't yet been
    generalized). Any field added to `Decision` in the future that `Executor.execute()` branches on
    must be added to `_canonical`/`_mint` in the same change that adds the field, not after."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._secret = secrets.token_bytes(32)

    def _canonical(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str, artifact_digest: str
    ) -> bytes:
        return f"{intent_id}:{principal_id}:{kind}:{permitted}:{execution_class}:{artifact_digest}".encode("utf-8")

    def _mint(
        self, intent_id: str, principal_id: str, kind: str, permitted: bool, execution_class: str, artifact_digest: str
    ) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted, execution_class, artifact_digest)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        permitted, execution_class = self._policy.evaluate(intent)
        artifact_digest = digest_of(intent.artifact_code) if intent.artifact_code is not None else ""
        token = self._mint(intent.intent_id, intent.principal_id, intent.kind, permitted, execution_class, artifact_digest)
        return Decision(
            intent_id=intent.intent_id,
            principal_id=intent.principal_id,
            kind=intent.kind,
            permitted=permitted,
            execution_class=execution_class,
            artifact_digest=artifact_digest,
            token=token,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id,
            decision.principal_id,
            decision.kind,
            decision.permitted,
            decision.execution_class,
            decision.artifact_digest,
        )
        return hmac.compare_digest(expected, decision.token)


class GateViolation(PermissionError):
    """A Decision failed verification, was denied by policy, or does not correspond to the Intent
    it's being used with. Base class for more specific violations raised elsewhere (e.g.
    execution.py's ArtifactMismatchError)."""
