"""Result types and the verdict rule.

Pre-registration mapping: the "Evidence categories" table, the "Pre-registered success criteria"
list, and the INCONCLUSIVE clause "Any bypass attempt fails for a reason *other than* the predicted
boundary -- this is inconclusive for that case, not a pass, because the criterion is about the
mechanism, not the outcome."

The single most important thing in this file is `verdict_for()`. It makes "the attempt raised an
exception, therefore PASS" unrepresentable:

- a PASS requires the OBSERVED mechanism to equal the mechanism PREDICTED IN ADVANCE;
- a PASS requires substrate mutation to be affirmatively observed as False, by an independent
  channel. `None` means "nobody checked", which is INCONCLUSIVE, never PASS. R cannot self-certify
  the absence of a Pod it has no authority to look for -- that is K-live evidence and it comes from
  the observer.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Category(str, Enum):
    """Evidence categories, exactly as pre-registered. E_BPF is defined for completeness of the
    vocabulary and is deliberately unused: the pre-registration drops kernel evidence from this
    experiment's minimum design because it bears on no bypass criterion."""

    S = "S"                # Siphonophore internal claim
    O = "O"                # OS authority fact
    K_AUTHZ = "K-authz"    # Kubernetes authentication/authorization fact
    K_LIVE = "K-live"      # Kubernetes live object state
    K_AUDIT = "K-audit"    # Kubernetes audit fact
    D = "D"                # derived correlation
    E_BPF = "E-bpf"        # kernel/eBPF -- not used by this experiment


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


# Mechanism constants. Shared so a probe and its criterion cannot drift apart by a typo.
MECH_K8S_AUTHN_REJECTED = "k8s_authn_rejected"
MECH_K8S_AUTHZ_REJECTED = "k8s_authz_rejected"
MECH_K8S_ACCEPTED = "k8s_accepted"
MECH_K8S_OTHER_STATUS = "k8s_other_status"
# Case-level roll-up of the two attempt-level rejections. Both 401 and 403 are the SAME
# predicted boundary ("Kubernetes refused R"); distinguishing them at case level would mean
# choosing the expected boundary after seeing which one occurred.
MECH_K8S_REJECTED = "k8s_authn_or_authz_rejected"
MECH_TLS_VERIFY_FAILED = "tls_verify_failed"
MECH_CONNECT_FAILED = "connect_failed"
MECH_NO_CONNECTION_INFO = "no_connection_info"

MECH_NO_CREDENTIAL_CONFIGURED = "no_credential_configured"
MECH_KUBECTL_BINARY_MISSING = "kubectl_binary_missing"
MECH_CLUSTER_UNREACHABLE = "cluster_unreachable"
MECH_POD_CREATED = "pod_created"
MECH_LOCAL_FAILURE = "local_failure"
# Case-level roll-up for bypass case B. The prediction is "R has no Kubernetes authority",
# which has two acceptable observable forms -- no credential configured at all, or a
# credential-less request refused at the API server. Rolling them into one constant keeps
# `expected_boundary` fixed in advance instead of chosen after seeing which form occurred.
MECH_SUBSTRATE_AUTHORITY_ABSENT = "substrate_authority_absent"

MECH_GATE_REJECTED_DECISION = "gate_rejected_decision"
MECH_ARTIFACT_DIGEST_REJECTED = "artifact_digest_rejected"
MECH_BACKEND_INVOKED = "backend_invoked"

MECH_PROTOCOL_REJECTED = "protocol_rejected"
MECH_PROTOCOL_ACCEPTED = "protocol_accepted"

MECH_CREDENTIAL_NOT_READABLE = "credential_not_readable"
MECH_CREDENTIAL_READABLE = "credential_readable"

MECH_SA_TOKEN_UNAUTHORIZED = "sa_token_not_authorized_for_target"
MECH_SA_TOKEN_AUTHORIZED = "sa_token_authorized_for_target"
MECH_SA_TOKEN_ABSENT = "sa_token_absent"
MECH_UNKNOWN = "unknown"

# Mechanisms that are an outright refutation wherever they appear, regardless of what any case
# predicted. Kept as a module constant so no individual probe can quietly omit one.
GLOBAL_FAIL_MECHANISMS = frozenset({
    MECH_K8S_ACCEPTED, MECH_POD_CREATED, MECH_BACKEND_INVOKED,
    MECH_CREDENTIAL_READABLE, MECH_SA_TOKEN_AUTHORIZED, MECH_PROTOCOL_ACCEPTED,
})


def verdict_for(
    *, expected_boundary: str, observed_mechanism: str,
    substrate_mutation_observed: bool | None,
    extra_fail_mechanisms: frozenset[str] = frozenset(),
) -> Verdict:
    """The verdict rule. Order matters and is deliberate.

    1. Observed substrate mutation refutes the case outright, whatever the mechanism said.
    2. A globally-refuting mechanism (or a case-specific one) is a FAIL.
    3. Unknown mutation status cannot be a PASS -- nobody checked.
    4. A PASS requires the predicted boundary, not merely some failure.
    """
    if substrate_mutation_observed is True:
        return Verdict.FAIL
    if observed_mechanism in GLOBAL_FAIL_MECHANISMS or observed_mechanism in extra_fail_mechanisms:
        return Verdict.FAIL
    if substrate_mutation_observed is None:
        return Verdict.INCONCLUSIVE
    if observed_mechanism == expected_boundary:
        return Verdict.PASS
    return Verdict.INCONCLUSIVE


@dataclass(frozen=True)
class CaseResult:
    """One bypass/falsification case. Serialized verbatim into the attempt's evidence directory."""

    case_id: str
    description: str
    attempted_path: str
    expected_boundary: str
    observed_mechanism: str
    substrate_mutation_observed: bool | None
    evidence_categories: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    verdict: Verdict = Verdict.INCONCLUSIVE
    observations: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verdict"] = self.verdict.value
        return data


def build_case(
    *, case_id: str, description: str, attempted_path: str, expected_boundary: str,
    observed_mechanism: str, substrate_mutation_observed: bool | None,
    evidence_categories: tuple[Category, ...], evidence_refs: tuple[str, ...] = (),
    observations: dict[str, Any] | None = None, notes: str = "",
    extra_fail_mechanisms: frozenset[str] = frozenset(),
) -> CaseResult:
    """Construct a CaseResult with the verdict DERIVED, never passed in. There is deliberately no
    way to hand-set a verdict on a case: that is how "it failed, call it a pass" gets in."""
    return CaseResult(
        case_id=case_id,
        description=description,
        attempted_path=attempted_path,
        expected_boundary=expected_boundary,
        observed_mechanism=observed_mechanism,
        substrate_mutation_observed=substrate_mutation_observed,
        evidence_categories=tuple(c.value for c in evidence_categories),
        evidence_refs=evidence_refs,
        verdict=verdict_for(
            expected_boundary=expected_boundary,
            observed_mechanism=observed_mechanism,
            substrate_mutation_observed=substrate_mutation_observed,
            extra_fail_mechanisms=extra_fail_mechanisms,
        ),
        observations=dict(observations or {}),
        notes=notes,
    )


def with_substrate_evidence(case: CaseResult, *, mutation_observed: bool, evidence_ref: str) -> CaseResult:
    """Attach the observer's K-live finding to a case R could only half-complete, and RE-DERIVE the
    verdict. This is the two-phase shape the evidence model requires: R records a mechanism, the
    independent observer records whether the substrate changed, and only the combination has a
    verdict."""
    return build_case(
        case_id=case.case_id,
        description=case.description,
        attempted_path=case.attempted_path,
        expected_boundary=case.expected_boundary,
        observed_mechanism=case.observed_mechanism,
        substrate_mutation_observed=mutation_observed,
        evidence_categories=tuple(Category(c) for c in case.evidence_categories) + (Category.K_LIVE,),
        evidence_refs=case.evidence_refs + (evidence_ref,),
        observations=case.observations,
        notes=case.notes,
    )


def summarize(cases: list[CaseResult]) -> dict[str, Any]:
    counts = {v.value: 0 for v in Verdict}
    for case in cases:
        counts[case.verdict.value] += 1
    return {
        "counts": counts,
        "all_pass": counts[Verdict.FAIL.value] == 0 and counts[Verdict.INCONCLUSIVE.value] == 0,
        "any_fail": counts[Verdict.FAIL.value] > 0,
        "cases": {c.case_id: c.verdict.value for c in cases},
    }
