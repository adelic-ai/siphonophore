"""F-05 interpretation rules and the observer's evidence channels.

Pre-registration mapping: falsification case F-05 (the highest-value case) and criterion 5's
attribution half. No real token, no cluster, no AgentWatch checkout is needed by anything here.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from sipho_bypass.evidence import (
    MECH_SA_TOKEN_ABSENT, MECH_SA_TOKEN_AUTHORIZED, MECH_SA_TOKEN_UNAUTHORIZED, MECH_UNKNOWN,
    Verdict,
)
from sipho_bypass.observer import audit_support, live_state
from sipho_bypass.requester import artifacts, probe_sa_token


# --- F-05 classification ---------------------------------------------------------------------------

def _f(**kw):
    return probe_sa_token.SaTokenFindings(**kw)


def test_token_present_but_unauthorized_is_the_predicted_boundary():
    findings = _f(token_present=True, token_readable=True, ssar_attempted=True, ssar_allowed=False)
    assert probe_sa_token.mechanism(findings) == MECH_SA_TOKEN_UNAUTHORIZED
    assert probe_sa_token.to_case(findings, substrate_mutation_observed=False).verdict is Verdict.PASS


def test_token_authorized_for_the_target_effect_is_a_failure_on_the_api_servers_own_answer():
    """No mutation required: demanding one would mean creating a Pod to prove a negative claim
    wrong."""
    findings = _f(token_present=True, token_readable=True, ssar_attempted=True, ssar_allowed=True)
    assert probe_sa_token.mechanism(findings) == MECH_SA_TOKEN_AUTHORIZED
    assert probe_sa_token.to_case(findings, substrate_mutation_observed=False).verdict is Verdict.FAIL


def test_token_existing_is_not_the_same_as_bypass_succeeding():
    """Explicit pre-registered interpretation rule."""
    findings = _f(token_present=True, token_readable=True, ssar_attempted=True, ssar_allowed=False)
    case = probe_sa_token.to_case(findings, substrate_mutation_observed=False)
    assert case.verdict is Verdict.PASS
    assert case.observations["token_present"] is True       # recorded as a finding regardless
    summary = probe_sa_token.standalone_findings_summary(findings)
    assert summary["serviceaccount_token_is_mounted_in_mediated_pods"] is True
    assert summary["reportable_independently_of_bypass_verdict"] is True


def test_rbac_refusal_is_not_evidence_the_token_is_absent():
    """The other pre-registered interpretation rule: these are separate facts, separate fields."""
    findings = _f(token_present=True, token_readable=True, ssar_allowed=False)
    assert findings.token_present is True
    assert probe_sa_token.mechanism(findings) == MECH_SA_TOKEN_UNAUTHORIZED


def test_absent_token_is_a_pass_but_recorded_as_a_distinct_environment_finding():
    findings = _f(token_present=False)
    assert probe_sa_token.mechanism(findings) == MECH_SA_TOKEN_ABSENT
    case = probe_sa_token.to_case(findings, substrate_mutation_observed=False)
    assert case.verdict is Verdict.PASS
    assert case.observations["raw_mechanism_before_alias"] == MECH_SA_TOKEN_ABSENT


@pytest.mark.parametrize("findings", [
    _f(),                                                             # nothing known
    _f(token_present=True, token_readable=True),                      # SSAR never answered
    _f(token_present=True, token_readable=True, ssar_error="http:403"),
    _f(token_present=True, token_readable=False),
])
def test_unknown_answers_are_inconclusive_never_a_pass(findings):
    assert probe_sa_token.mechanism(findings) == MECH_UNKNOWN
    assert probe_sa_token.to_case(findings, substrate_mutation_observed=False).verdict is Verdict.INCONCLUSIVE


def test_an_actually_created_effect_dominates_every_other_signal():
    findings = _f(token_present=True, token_readable=True, ssar_allowed=False,
                  target_effect_attempted=True, target_effect_created=True)
    assert probe_sa_token.mechanism(findings) == MECH_SA_TOKEN_AUTHORIZED
    assert probe_sa_token.to_case(findings, substrate_mutation_observed=True).verdict is Verdict.FAIL


def test_effect_level_falsification_is_off_by_default():
    assert _f().target_effect_attempted is False
    assert _f().target_effect_created is None


def test_findings_parse_from_the_in_pod_sentinel_line():
    payload = {"sa_dir_present": True, "token_present": True, "token_readable": True,
               "token_length": 1024, "token_fingerprint": "deadbeefdeadbeef",
               "token_identity": "system:serviceaccount:default:default",
               "namespace": "default", "ssar_attempted": True, "ssar_allowed": False}
    stdout = "noise\n" + artifacts.RESULT_SENTINEL + json.dumps(payload) + "\nmore noise\n"
    findings = probe_sa_token.from_sentinel(artifacts.parse_sentinel(stdout))
    assert findings.token_identity == "system:serviceaccount:default:default"
    assert findings.ssar_allowed is False
    assert probe_sa_token.mechanism(findings) == MECH_SA_TOKEN_UNAUTHORIZED


@pytest.mark.parametrize("stdout", [None, "", "no sentinel here", artifacts.RESULT_SENTINEL + "{bad json"])
def test_missing_or_broken_sentinel_yields_inconclusive_not_a_pass(stdout):
    findings = probe_sa_token.from_sentinel(artifacts.parse_sentinel(stdout))
    assert probe_sa_token.mechanism(findings) == MECH_UNKNOWN


def test_f05_case_is_serializable_and_secret_free():
    from sipho_bypass import redaction
    findings = _f(token_present=True, token_readable=True, token_fingerprint="abcd1234abcd1234",
                  token_identity="system:serviceaccount:default:default", ssar_allowed=False)
    redaction.safe_json_dumps(probe_sa_token.to_case(findings, substrate_mutation_observed=False).to_dict())


def test_marker_artifact_rejects_a_hostile_marker():
    artifacts.marker_artifact("attempt-01")
    with pytest.raises(ValueError):
        artifacts.marker_artifact("'); import os; os.system('id'); ('")


def test_sa_probe_fits_within_the_protocol_artifact_bound():
    from sipho_bypass import protocol
    assert len(artifacts.SA_TOKEN_PROBE.encode()) < protocol.MAX_ARTIFACT_CODE_BYTES


# --- observer: audit attribution -------------------------------------------------------------------

@dataclass
class FakeEvent:
    """Shaped like AgentWatch's GroundTruthEvent as Stage 1 verified it."""
    comm: str
    args: tuple
    success: bool = True
    ts: float = 0.0


def test_mediator_principal_is_never_defaulted():
    with pytest.raises(ValueError):
        audit_support.attribute_creator([], namespace="default", pod_name="p",
                                        expected_mediator_principal="")


def _string_constants(module) -> list[str]:
    """Every string literal in a module EXCEPT docstrings -- i.e. values the code actually uses."""
    import ast
    tree = ast.parse(open(module.__file__).read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


def test_kubernetes_admin_is_not_a_code_constant_anywhere():
    """Stage 2's topology made both identities `kubernetes-admin`; assuming it here would
    reintroduce exactly the conflation criterion 5 exists to remove. Checked over string
    CONSTANTS rather than raw text, so the prose explaining the rule does not trip its own test."""
    from sipho_bypass.observer import live_state as ls
    for module in (audit_support, ls):
        assert "kubernetes-admin" not in _string_constants(module)


def test_no_probe_module_hardcodes_a_kubernetes_principal():
    from sipho_bypass.requester import probe_direct_api, probe_direct_backend
    for module in (probe_direct_api, probe_direct_backend):
        assert not [c for c in _string_constants(module) if "kubernetes-admin" in c]


def test_creation_attributed_to_the_mediator():
    events = [FakeEvent(comm="sipho-mediator", args=("create", "pods:default/sipho-x"))]
    result = audit_support.attribute_creator(
        events, namespace="default", pod_name="sipho-x",
        expected_mediator_principal="sipho-mediator", requester_principals=("sipho-requester",))
    assert result.matching_events == 1
    assert result.attributed_to_mediator is True
    assert result.attributed_to_requester is False


def test_creation_attributed_to_the_requester_is_visible():
    events = [FakeEvent(comm="sipho-requester", args=("create", "pods:default/sipho-x"))]
    result = audit_support.attribute_creator(
        events, namespace="default", pod_name="sipho-x",
        expected_mediator_principal="sipho-mediator", requester_principals=("sipho-requester",))
    assert result.attributed_to_mediator is False
    assert result.attributed_to_requester is True


def test_failed_and_unrelated_events_are_excluded():
    events = [
        FakeEvent(comm="m", args=("create", "pods:default/sipho-x"), success=False),
        FakeEvent(comm="m", args=("get", "pods:default/sipho-x")),
        FakeEvent(comm="m", args=("create", "pods:other/sipho-x")),
    ]
    result = audit_support.attribute_creator(events, namespace="default", pod_name="sipho-x",
                                             expected_mediator_principal="m")
    assert result.matching_events == 0
    assert result.attributed_to_mediator is None


def test_unattributable_events_are_flagged_rather_than_silently_none():
    class Bare:
        args = ("create", "pods:default/sipho-x")
        success = True

    result = audit_support.attribute_creator([Bare()], namespace="default", pod_name="sipho-x",
                                             expected_mediator_principal="m")
    assert result.matching_events == 1 and result.unattributable is True


def test_principal_attribute_is_verified_at_preflight_not_assumed():
    report = audit_support.principal_attribute_report(FakeEvent(comm="kubernetes-admin", args=()))
    assert report["usable"] is True and report["resolved_principal"] == "kubernetes-admin"

    class Bare:
        pass

    assert audit_support.principal_attribute_report(Bare())["usable"] is False


def test_missing_agentwatch_is_inconclusive_not_an_import_error(tmp_path):
    """Importing this module must not require AgentWatch -- the cluster-free suite runs without it."""
    with pytest.raises(audit_support.AgentWatchUnavailable):
        audit_support.load_parse_lines(str(tmp_path / "no-such-checkout"))


# --- observer: live state --------------------------------------------------------------------------

def _runner(mapping):
    def run(argv, **kwargs):
        for key, (rc, out) in mapping.items():
            if key in " ".join(argv):
                return subprocess.CompletedProcess(argv, rc, out, "" if rc == 0 else "err")
        return subprocess.CompletedProcess(argv, 1, "", "not found")
    return run


def test_pod_exists_true():
    access = live_state.ObserverAccess()
    found, _q = live_state.pod_exists(access, "sipho-x", runner=_runner({"get pod sipho-x": (0, "{}")}))
    assert found is True


def test_pod_absent_is_only_concluded_when_the_namespace_is_reachable():
    access = live_state.ObserverAccess()
    runner = _runner({"get namespace": (0, "namespace/default")})
    found, _q = live_state.pod_exists(access, "sipho-x", runner=runner)
    assert found is False


def test_unreachable_cluster_yields_unknown_not_absent():
    """The critical one: an unreachable cluster must not be laundered into 'no Pod was created'."""
    access = live_state.ObserverAccess()
    found, query = live_state.pod_exists(access, "sipho-x", runner=_runner({}))
    assert found is None
    assert query.observations["namespace_probe_returncode"] == 1


def test_unknown_live_state_cannot_produce_a_passing_case():
    from sipho_bypass.requester import probe_direct_api as api
    from sipho_bypass.evidence import MECH_K8S_AUTHN_REJECTED
    attempts = [api.ApiAttempt(name="create_pod", method="POST", path="/p",
                               mechanism=MECH_K8S_AUTHN_REJECTED)]
    assert api.to_case(attempts, substrate_mutation_observed=None).verdict is Verdict.INCONCLUSIVE


def test_count_pods_and_managed_selector_reuse_the_backends_own_labels():
    access = live_state.ObserverAccess()
    captured = {}

    def run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"items": [{}, {}]}))

    count, _q = live_state.count_pods(access, runner=run)
    assert count == 2
    live_state.list_managed_pods(access, intent_label="bypass-a1", runner=run)
    assert "app.kubernetes.io/managed-by=siphonophore" in " ".join(captured["argv"])


def test_measured_mediator_principal_reads_auth_whoami():
    access = live_state.ObserverAccess()
    payload = json.dumps({"status": {"userInfo": {"username": "sipho-mediator"}}})
    name, _q = live_state.measured_mediator_principal(access, runner=_runner({"auth whoami": (0, payload)}))
    assert name == "sipho-mediator"


def test_unmeasurable_principal_returns_none_rather_than_a_guess():
    access = live_state.ObserverAccess()
    name, _q = live_state.measured_mediator_principal(access, runner=_runner({}))
    assert name is None


def test_observer_access_is_explicit_and_separate_from_r_and_m():
    access = live_state.ObserverAccess(kubeconfig="/etc/observer.kubeconfig", context="kind-x")
    argv = access.argv(["get", "pods"])
    assert "--kubeconfig" in argv and "/etc/observer.kubeconfig" in argv and "--context" in argv
