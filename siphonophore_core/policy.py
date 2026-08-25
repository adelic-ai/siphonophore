"""Policy -- decides whether an Intent is permitted and which execution class it requires
(DESIGN.md section 2).

A real, pluggable interface -- not a fixed mapping. Every lab experiment inlined its own
`CONSEQUENCE_TO_CLASS` dict directly inside `Gate`. This is the first place that decision becomes
something a caller can actually replace, matching DESIGN.md section 6's framing of the policy
engine as a customizable mechanism, never baked into the Gate itself.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .intent import Intent


@dataclass(frozen=True)
class Decision:
    """An authorization minted by a Gate. Every field here that Executor dispatch branches on --
    `kind`, `execution_class`, `artifact_digest` -- must be (and is, by `Gate._mint`) bound into
    `token`. This is a standing rule discovered twice independently while building the lab
    experiments (lab/002 for `kind`, lab/003 for `execution_class`) and restated here as a hard
    requirement for any field added to this class in the future: bind it into the token at the
    moment it's added, not after."""

    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str
    artifact_digest: str
    token: str


class Policy(ABC):
    """Decides `permitted` and `execution_class` for an Intent. Deliberately does not decide
    `artifact_digest` (always `sha256(intent.artifact_code)`, computed by `Gate` itself -- not
    policy-dependent) and never touches the cryptographic token (kept out of pluggable code,
    Gate's own responsibility alone)."""

    @abstractmethod
    def evaluate(self, intent: Intent) -> tuple[bool, str]:
        """Returns (permitted, execution_class)."""
        ...


class ConsequencePolicy(Policy):
    """The policy every lab experiment actually used, given a real home rather than left inlined.

    Disclosed limitation, not silently fixed here: this trusts `intent.consequence` as-is -- a
    caller-declared field, not something independently determined. DESIGN.md's "Explicitly open"
    section names replacing this as unresolved. This class exists so the behavior lab/001-009
    already validated has somewhere real to live, not as a claim that the underlying problem is
    solved."""

    DEFAULT_MAPPING = {
        "low": "same_process",
        "high": "separate_process",
        "privileged": "uid_cgroup",
    }
    DEFAULT_ALLOWED_KINDS = ("write_file", "run_artifact", "delegate")

    def __init__(
        self,
        mapping: dict[str, str] | None = None,
        allowed_kinds: tuple[str, ...] | None = None,
    ) -> None:
        self._mapping = dict(mapping) if mapping is not None else dict(self.DEFAULT_MAPPING)
        self._allowed_kinds = allowed_kinds if allowed_kinds is not None else self.DEFAULT_ALLOWED_KINDS

    def evaluate(self, intent: Intent) -> tuple[bool, str]:
        permitted = intent.kind in self._allowed_kinds
        execution_class = self._mapping.get(intent.consequence, "same_process")
        return permitted, execution_class
