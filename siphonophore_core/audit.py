"""Belnap four-valued reconciliation (DESIGN.md section 3; lab/007).

Two independent channels observe what a principal did: self-report (the agent's own, untrusted
narrative -- useful for explainability, worthless as ground truth on its own) and ground truth
(established by an observer outside the agent's trust domain, per DESIGN.md section 5). Comparing
them as a single match/no-match boolean throws away the distinction between a lie (claimed, not
observed) and an omission (observed, not claimed) -- both collapse to the same "mismatch". Belnap's
four-valued logic keeps them apart:

    claimed  observed
      T         T      -> corroborated
      T         F      -> contradiction        (a claim the ground truth does not support)
      F         T      -> unreported_activity   (something happened that was never claimed)
      F         F      -> no_evidence

Consolidates lab/007 (the first real implementation of this table, and the first experiment in this
repo where self-report and ground truth were made to genuinely disagree -- via a real delegated
sub-agent, under its own provisioned uid, that actively lied about one file's content and performed
an extra undisclosed write) into the package's permanent audit primitive. `reconcile()` and
`reconcile_path()` are pure Python -- no sockets, no subprocess, no Linux dependency -- and take
their two inputs already independently produced; reconciliation happens strictly above both claims,
never inside either one, so it cannot be biased toward trusting one channel over the other.

`collect_ground_truth()` is the "external observer" half DESIGN.md section 5 describes: a plain
directory listing and file reads, nothing self-report-aware. Genuine root-required delegation
scenarios (a real uid_cgroup sub-agent lying about its own effects) are exercised in
test_audit_linux.py; this module itself needs neither root nor Linux.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class BelnapValue(Enum):
    CORROBORATED = "corroborated"
    CONTRADICTION = "contradiction"
    UNREPORTED_ACTIVITY = "unreported_activity"
    NO_EVIDENCE = "no_evidence"


def reconcile(claimed: bool, observed: bool) -> BelnapValue:
    """The four-valued comparison DESIGN.md section 3 names. Takes two already-independently-
    produced booleans and does nothing else -- it has no access to how `claimed` or `observed`
    were derived, which is the point: it cannot be biased toward trusting one channel over the
    other."""
    if claimed and observed:
        return BelnapValue.CORROBORATED
    if claimed and not observed:
        return BelnapValue.CONTRADICTION
    if not claimed and observed:
        return BelnapValue.UNREPORTED_ACTIVITY
    return BelnapValue.NO_EVIDENCE


@dataclass(frozen=True)
class Claim:
    path: str
    content: str


@dataclass(frozen=True)
class SelfReport:
    """The agent's own, untrusted narrative (DESIGN.md section 3). Nothing here is verified; it is
    exactly what the principal's own process asserted about itself."""
    principal_id: str
    claims: tuple[Claim, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "SelfReport":
        claims = tuple(Claim(path=c["path"], content=c["content"]) for c in data.get("claims", []))
        return cls(principal_id=data.get("principal_id", "unknown"), claims=claims)


def collect_ground_truth(outdir: Path) -> dict[str, str]:
    """The external observer's own read (DESIGN.md section 5): a plain directory listing and file
    reads, nothing self-report-aware. Caller is responsible for calling this only after
    independently confirming the principal's process has actually exited (e.g. proc.wait()) --
    reading ground truth from a still-running principal is a race, not an observation."""
    ground_truth: dict[str, str] = {}
    for entry in outdir.iterdir():
        if entry.is_file():
            ground_truth[entry.name] = entry.read_text()
    return ground_truth


def reconcile_path(self_report: SelfReport, path: str, expected_content: str | None,
                    ground_truth: dict[str, str]) -> dict:
    """Reconcile one proposition about `path` against the self-report and ground truth.

    If `expected_content` is given, the proposition under test is "self_report claims path has
    exactly this content" vs. "ground truth shows path has exactly this content" -- this is how a
    genuine contradiction (T/F) is detected: the self-report can claim a path with SOME content
    while ground truth shows different content at that same path, which is claimed=True,
    observed=False for the specific claimed proposition, not merely "path exists or not".

    If `expected_content` is None, the proposition under test is simply "path was claimed at all"
    vs. "path exists in ground truth at all" -- used for the unreported-activity and no-evidence
    cases, where the whole point is that no claim mentions the path."""
    claim_for_path = next((c for c in self_report.claims if c.path == path), None)
    if expected_content is not None:
        claimed = claim_for_path is not None and claim_for_path.content == expected_content
        observed = ground_truth.get(path) == expected_content
    else:
        claimed = claim_for_path is not None
        observed = path in ground_truth
    value = reconcile(claimed, observed)
    return {
        "path": path, "claimed": claimed, "observed": observed, "value": value.value,
        "self_reported_content": claim_for_path.content if claim_for_path else None,
        "ground_truth_content": ground_truth.get(path),
    }
