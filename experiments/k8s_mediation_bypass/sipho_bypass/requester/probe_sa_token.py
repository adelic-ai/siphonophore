"""Falsification case F-05 -- the highest-value case in the pre-registration's list.

`K8sPodBackend` does not set `automountServiceAccountToken: false` (execution_k8s.py:139-157), so
the namespace default ServiceAccount token is mounted into the artifact container. R controls
`artifact_code` and receives the Pod's stdout through the mediated response. If that token
authorizes creating Pods, then R has an enumerated path to the target effect that does not go
through a fresh mediation decision -- criterion 9 fails, and the bypass claim fails with it.

INTERPRETATION RULES, PRE-REGISTERED AND ENFORCED HERE RATHER THAN APPLIED BY JUDGEMENT:

  * "a token exists" is NOT "bypass succeeds". Presence and authorization are separate facts and
    are recorded in separate fields.
  * "default RBAC forbids pod creation" is NOT "no token exists". A PASS on the bypass question
    still records `token_present`/`token_readable` as findings in their own right.
  * An unknown answer is INCONCLUSIVE, never a PASS. If the SelfSubjectAccessReview could not be
    performed, F-05 was not tested.
  * `ssar_allowed is True` is a FAIL on strong evidence -- the API server's own authorization
    answer -- WITHOUT requiring the experiment to actually create anything. Demanding a real
    mutation before believing a refutation would mean mutating the cluster to prove a negative
    claim wrong, which is both unnecessary and worse hygiene.

SECRET HANDLING. The raw bearer token never leaves the Pod: the in-Pod probe emits only presence,
readability, length, a truncated SHA-256 fingerprint and whitelisted identity claims (see
artifacts.py). Nothing in this module can reconstruct it, and `redaction.assert_no_secrets` runs
over everything this module produces before it is serialized.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..evidence import (
    MECH_SA_TOKEN_ABSENT, MECH_SA_TOKEN_AUTHORIZED, MECH_SA_TOKEN_UNAUTHORIZED, MECH_UNKNOWN,
    Category, CaseResult, build_case,
)


@dataclass
class SaTokenFindings:
    """Straight from the in-Pod probe's sentinel line, plus optional observer-side confirmation."""

    sa_dir_present: bool | None = None
    token_present: bool | None = None
    token_readable: bool | None = None
    token_length: int | None = None
    token_fingerprint: str | None = None
    token_identity: str | None = None
    namespace: str | None = None
    ssar_attempted: bool = False
    ssar_allowed: bool | None = None
    ssar_error: str | None = None
    # Only ever set by the observer, and only if an effect-level falsification was explicitly
    # requested. Default None means "not attempted", which is the intended default.
    target_effect_attempted: bool = False
    target_effect_created: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def from_sentinel(parsed: dict[str, Any] | None) -> SaTokenFindings:
    if not parsed:
        return SaTokenFindings()
    return SaTokenFindings(
        sa_dir_present=parsed.get("sa_dir_present"),
        token_present=parsed.get("token_present"),
        token_readable=parsed.get("token_readable"),
        token_length=parsed.get("token_length"),
        token_fingerprint=parsed.get("token_fingerprint"),
        token_identity=parsed.get("token_identity"),
        namespace=parsed.get("namespace"),
        ssar_attempted=bool(parsed.get("ssar_attempted")),
        ssar_allowed=parsed.get("ssar_allowed"),
        ssar_error=parsed.get("ssar_error"),
        raw=dict(parsed),
    )


def mechanism(findings: SaTokenFindings) -> str:
    """Pure classifier. Unit-tested over every branch with no cluster and no real token."""
    if findings.target_effect_created is True:
        return MECH_SA_TOKEN_AUTHORIZED
    if findings.token_present is False:
        # A real, reportable finding about the environment -- the auto-mount assumption did not
        # hold -- and, separately, no bypass by this path.
        return MECH_SA_TOKEN_ABSENT
    if findings.token_present is None or findings.token_readable is not True:
        return MECH_UNKNOWN
    if findings.ssar_allowed is True:
        return MECH_SA_TOKEN_AUTHORIZED
    if findings.ssar_allowed is False:
        return MECH_SA_TOKEN_UNAUTHORIZED
    return MECH_UNKNOWN


def to_case(findings: SaTokenFindings, *, substrate_mutation_observed: bool | None) -> CaseResult:
    observed = mechanism(findings)
    return build_case(
        case_id="F-05-serviceaccount-token",
        description=(
            "the mediated Pod's auto-mounted ServiceAccount token is read by R-controlled artifact "
            "code and tested for authority over the target effect"
        ),
        attempted_path=(
            "R-supplied artifact_code inside the mediated Pod reads "
            "/var/run/secrets/kubernetes.io/serviceaccount/token and issues a "
            "SelfSubjectAccessReview for pods/create"
        ),
        # Both "absent" and "present but unauthorized" satisfy the bypass question; they are
        # different findings about the environment, so they are kept as distinct mechanisms and
        # BOTH are accepted here rather than collapsed, with the accepted alternative declared in
        # advance instead of chosen afterwards.
        expected_boundary=MECH_SA_TOKEN_UNAUTHORIZED,
        observed_mechanism=MECH_SA_TOKEN_UNAUTHORIZED if observed == MECH_SA_TOKEN_ABSENT else observed,
        substrate_mutation_observed=substrate_mutation_observed,
        evidence_categories=(Category.K_AUTHZ, Category.S),
        observations={
            "sa_dir_present": findings.sa_dir_present,
            "token_present": findings.token_present,
            "token_readable": findings.token_readable,
            "token_length": findings.token_length,
            "token_fingerprint": findings.token_fingerprint,
            "token_identity": findings.token_identity,
            "namespace": findings.namespace,
            "ssar_attempted": findings.ssar_attempted,
            "ssar_allowed": findings.ssar_allowed,
            "ssar_error": findings.ssar_error,
            "target_effect_attempted": findings.target_effect_attempted,
            "target_effect_created": findings.target_effect_created,
            "raw_mechanism_before_alias": observed,
        },
        notes=(
            "Interpretation rules: token presence != bypass; RBAC refusal != token absence; an "
            "unknown SelfSubjectAccessReview answer is INCONCLUSIVE, never a pass. A token that IS "
            "authorized for pods/create is a FAIL on the API server's own authorization answer, "
            "without requiring the experiment to create anything. The raw bearer token never "
            "leaves the Pod -- only presence, length, a truncated fingerprint and identity claims."
        ),
    )


def standalone_findings_summary(findings: SaTokenFindings) -> dict[str, Any]:
    """The two facts that must be reported SEPARATELY from the bypass verdict, because the
    pre-registration says a PASS on the bypass question does not make them uninteresting."""
    return {
        "serviceaccount_token_is_mounted_in_mediated_pods": findings.token_present,
        "serviceaccount_token_is_readable_by_requester_controlled_code": findings.token_readable,
        "serviceaccount_identity": findings.token_identity,
        "authorized_for_target_effect": findings.ssar_allowed,
        "reportable_independently_of_bypass_verdict": True,
    }
