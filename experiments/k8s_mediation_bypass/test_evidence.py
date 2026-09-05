"""The verdict rule -- the single most important piece of the evidence model.

Pre-registration mapping: the INCONCLUSIVE clause "Any bypass attempt fails for a reason *other
than* the predicted boundary -- this is inconclusive for that case, not a pass", and the FAIL
condition "any denied or bypass attempt mutates Kubernetes state at all".
"""
from __future__ import annotations

import pytest

from sipho_bypass import evidence
from sipho_bypass.evidence import Category, Verdict, build_case, verdict_for


def test_pass_requires_the_predicted_boundary():
    assert verdict_for(expected_boundary="a", observed_mechanism="a",
                       substrate_mutation_observed=False) is Verdict.PASS


def test_failing_for_the_wrong_reason_is_inconclusive_not_a_pass():
    """The rule that makes 'any exception = PASS' unrepresentable."""
    assert verdict_for(expected_boundary="k8s_authn_or_authz_rejected",
                       observed_mechanism="connect_failed",
                       substrate_mutation_observed=False) is Verdict.INCONCLUSIVE


def test_unknown_substrate_state_can_never_be_a_pass():
    """R cannot self-certify the absence of a Pod it has no authority to look for."""
    assert verdict_for(expected_boundary="a", observed_mechanism="a",
                       substrate_mutation_observed=None) is Verdict.INCONCLUSIVE


def test_observed_mutation_fails_even_when_the_mechanism_matched():
    assert verdict_for(expected_boundary="a", observed_mechanism="a",
                       substrate_mutation_observed=True) is Verdict.FAIL


@pytest.mark.parametrize("mechanism", sorted(evidence.GLOBAL_FAIL_MECHANISMS))
def test_globally_refuting_mechanisms_always_fail(mechanism):
    """Even if a case were mis-written to predict one of these, it still fails."""
    assert verdict_for(expected_boundary=mechanism, observed_mechanism=mechanism,
                       substrate_mutation_observed=False) is Verdict.FAIL


def test_case_specific_fail_mechanisms_are_honoured():
    assert verdict_for(expected_boundary="a", observed_mechanism="b",
                       substrate_mutation_observed=False,
                       extra_fail_mechanisms=frozenset({"b"})) is Verdict.FAIL


def test_a_case_cannot_be_handed_a_verdict():
    """`build_case` derives the verdict; there is deliberately no parameter for it."""
    import inspect
    assert "verdict" not in inspect.signature(build_case).parameters


def _case(**kw):
    base = dict(case_id="X", description="d", attempted_path="p", expected_boundary="a",
                observed_mechanism="a", substrate_mutation_observed=False,
                evidence_categories=(Category.O,))
    base.update(kw)
    return build_case(**base)


def test_observer_evidence_re_derives_the_verdict():
    partial = _case(substrate_mutation_observed=None)
    assert partial.verdict is Verdict.INCONCLUSIVE
    completed = evidence.with_substrate_evidence(partial, mutation_observed=False, evidence_ref="live.json")
    assert completed.verdict is Verdict.PASS
    assert "K-live" in completed.evidence_categories
    assert "live.json" in completed.evidence_refs


def test_observer_evidence_can_turn_a_case_into_a_failure():
    partial = _case(substrate_mutation_observed=None)
    completed = evidence.with_substrate_evidence(partial, mutation_observed=True, evidence_ref="live.json")
    assert completed.verdict is Verdict.FAIL


def test_summary_counts_and_requires_zero_inconclusive_for_all_pass():
    cases = [_case(case_id="a"), _case(case_id="b", substrate_mutation_observed=None)]
    summary = evidence.summarize(cases)
    assert summary["counts"] == {"PASS": 1, "FAIL": 0, "INCONCLUSIVE": 1}
    assert summary["all_pass"] is False
    assert summary["any_fail"] is False


def test_case_serializes_to_plain_json_types():
    import json
    json.dumps(_case().to_dict())


def test_ebpf_category_exists_but_is_unused_by_design():
    """The pre-registration drops kernel evidence from this experiment's minimum design."""
    assert Category.E_BPF.value == "E-bpf"
    from sipho_bypass.requester import probe_direct_api, probe_direct_backend, probe_sdk, probe_sa_token
    from sipho_bypass.requester import authority_snapshot
    for module in (probe_direct_api, probe_direct_backend, probe_sdk, probe_sa_token, authority_snapshot):
        assert "E_BPF" not in open(module.__file__).read()
