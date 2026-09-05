"""Classifier tests for the two live bypass probes -- run with no network and no cluster.

Pre-registration mapping: criteria 2 and 3, and the INCONCLUSIVE rule that a failure for an
unpredicted reason is not a pass. Both classifiers are pure functions precisely so they can be
exercised exhaustively here rather than only once, live, against a real cluster.
"""
from __future__ import annotations

import subprocess

import pytest

from sipho_bypass.evidence import (
    MECH_CLUSTER_UNREACHABLE, MECH_CONNECT_FAILED, MECH_K8S_ACCEPTED, MECH_K8S_AUTHN_REJECTED,
    MECH_K8S_AUTHZ_REJECTED, MECH_K8S_OTHER_STATUS, MECH_K8S_REJECTED, MECH_KUBECTL_BINARY_MISSING,
    MECH_LOCAL_FAILURE, MECH_NO_CONNECTION_INFO, MECH_NO_CREDENTIAL_CONFIGURED, MECH_POD_CREATED,
    MECH_SUBSTRATE_AUTHORITY_ABSENT, MECH_TLS_VERIFY_FAILED, Verdict,
)
from sipho_bypass.requester import probe_direct_api as api
from sipho_bypass.requester import probe_direct_backend as backend


# --- case A: direct Kubernetes API ---------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (200, MECH_K8S_ACCEPTED), (201, MECH_K8S_ACCEPTED), (299, MECH_K8S_ACCEPTED),
    (401, MECH_K8S_AUTHN_REJECTED), (403, MECH_K8S_AUTHZ_REJECTED),
    (404, MECH_K8S_OTHER_STATUS), (500, MECH_K8S_OTHER_STATUS), (409, MECH_K8S_OTHER_STATUS),
])
def test_http_status_classification(status, expected):
    assert api.classify(status=status, transport_error_kind=None) == expected


@pytest.mark.parametrize("kind,expected", [
    ("tls", MECH_TLS_VERIFY_FAILED),
    ("connect", MECH_CONNECT_FAILED),
    ("no_connection_info", MECH_NO_CONNECTION_INFO),
])
def test_transport_failures_are_never_authorization_evidence(kind, expected):
    """The distinction this whole module exists to preserve: 'R could not reach the API server' is
    not 'Kubernetes denied R'."""
    assert api.classify(status=None, transport_error_kind=kind) == expected


def test_201_created_is_not_mistaken_for_a_rejection():
    """A `== 200` check would have missed the single most important failure mode: the API server
    accepting R's Pod creation."""
    assert api.classify(status=201, transport_error_kind=None) == MECH_K8S_ACCEPTED


def test_case_takes_the_weakest_enumerated_outcome():
    assert api.case_mechanism([MECH_K8S_AUTHN_REJECTED, MECH_CONNECT_FAILED]) == MECH_CONNECT_FAILED
    assert api.case_mechanism([MECH_K8S_AUTHN_REJECTED, MECH_K8S_AUTHZ_REJECTED]) == MECH_K8S_REJECTED
    assert api.case_mechanism([MECH_K8S_AUTHZ_REJECTED, MECH_K8S_ACCEPTED]) == MECH_K8S_ACCEPTED
    assert api.case_mechanism([]) == MECH_NO_CONNECTION_INFO


def test_case_a_verdicts():
    def attempts(*mechs):
        return [api.ApiAttempt(name=f"a{i}", method="POST", path="/p", mechanism=m)
                for i, m in enumerate(mechs)]

    rejected = api.to_case(attempts(MECH_K8S_AUTHN_REJECTED, MECH_K8S_AUTHZ_REJECTED),
                           substrate_mutation_observed=False)
    assert rejected.verdict is Verdict.PASS

    accepted = api.to_case(attempts(MECH_K8S_ACCEPTED), substrate_mutation_observed=False)
    assert accepted.verdict is Verdict.FAIL

    unreachable = api.to_case(attempts(MECH_CONNECT_FAILED), substrate_mutation_observed=False)
    assert unreachable.verdict is Verdict.INCONCLUSIVE

    unchecked = api.to_case(attempts(MECH_K8S_AUTHN_REJECTED), substrate_mutation_observed=None)
    assert unchecked.verdict is Verdict.INCONCLUSIVE


def test_no_connection_info_is_inconclusive_not_a_pass():
    result = api.attempt(None, name="create_pod", method="POST", path="/p")
    assert result.mechanism == MECH_NO_CONNECTION_INFO
    assert api.to_case([result], substrate_mutation_observed=False).verdict is Verdict.INCONCLUSIVE


def test_no_authorization_header_is_ever_sent():
    """R has no bearer token under the threat model. Adding one here would silently change what the
    experiment measures."""
    source = open(api.__file__).read()
    assert "add_header(\"Authorization\"" not in source


def test_attempt_uses_the_injected_transport_and_never_touches_the_network():
    seen = {}

    class FakeResponse:
        status = 403

        def read(self, n=None):
            return b'{"reason":"Forbidden","message":"pods is forbidden: User \\"x\\""}'

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def transport(info, method, path, body, timeout):
        seen.update({"method": method, "path": path, "body": body})
        from urllib import error as urlerror
        raise urlerror.HTTPError(path, 403, "Forbidden", {}, FakeResponse())

    info = api.ConnectionInfo(api_server="https://198.51.100.1:6443")
    result = api.attempt(info, name="create_pod", method="POST", path="/api/v1/x", body={"a": 1},
                         transport=transport)
    assert result.status == 403
    assert result.mechanism == MECH_K8S_AUTHZ_REJECTED
    assert result.k8s_reason == "Forbidden"
    # The Status `message` embeds the requesting identity, so only its PRESENCE is recorded.
    assert result.k8s_status_message_present is True
    assert not hasattr(result, "k8s_status_message")
    assert seen["method"] == "POST"


def test_enumerated_surface_covers_f13_alternate_namespace_and_kind():
    calls = []

    def transport(info, method, path, body, timeout):
        calls.append(path)
        raise OSError("no network in this test")

    api.run_all(api.ConnectionInfo(api_server="https://198.51.100.1:6443"),
                pod_name="probe-1", namespace="default", alt_namespace="kube-public",
                transport=transport)
    assert any("/namespaces/default/pods" in p for p in calls)
    assert any("/namespaces/kube-public/pods" in p for p in calls)
    assert any("/jobs" in p for p in calls)


def test_probe_manifest_is_not_built_by_siphonophore():
    """R is modelled as an independent attacker writing its own manifest; reusing the backend's
    builder would make the probe depend on the component it is bypassing."""
    source = open(api.__file__).read()
    assert "execution_k8s" not in source
    manifest = api.target_pod_manifest("p", "default")
    assert manifest["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "bypass-probe"


# --- case B: direct backend ------------------------------------------------------------------------

@pytest.mark.parametrize("stderr,expected", [
    ("error: no configuration has been provided, try setting KUBERNETES_MASTER",
     MECH_NO_CREDENTIAL_CONFIGURED),
    ("error: invalid configuration: no configuration has been provided", MECH_NO_CREDENTIAL_CONFIGURED),
    ("error: open /home/r/.kube/config: no such file or directory", MECH_NO_CREDENTIAL_CONFIGURED),
    ("error: You must be logged in to the server (Unauthorized)", MECH_NO_CREDENTIAL_CONFIGURED),
    ('Error from server (Forbidden): pods is forbidden: User "system:anonymous" cannot create resource',
     MECH_K8S_REJECTED),
    ("Unable to connect to the server: x509: certificate signed by unknown authority",
     MECH_TLS_VERIFY_FAILED),
    # A refused connection to a REAL server address means the cluster is down: inconclusive.
    ("The connection to the server 127.0.0.1:6443 was refused - did you specify the right host?",
     MECH_CLUSTER_UNREACHABLE),
    # ...but kubectl's no-kubeconfig fallback targets localhost:8080, and that IS the
    # no-credential signature rather than a dead cluster.
    ("The connection to the server localhost:8080 was refused - did you specify the right host or port?",
     MECH_NO_CREDENTIAL_CONFIGURED),
    ("Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout", MECH_CLUSTER_UNREACHABLE),
    ("something entirely unexpected happened", MECH_LOCAL_FAILURE),
])
def test_kubectl_stderr_classification(stderr, expected):
    assert backend.classify_kubectl_stderr(returncode=1, stderr=stderr) == expected


def test_successful_kubectl_is_never_the_predicted_boundary():
    """rc=0 means R could read the cluster -- the separation premise is broken."""
    assert backend.classify_kubectl_stderr(returncode=0, stderr="") == MECH_LOCAL_FAILURE


def test_case_b_rolls_both_authority_absent_forms_into_one_expected_boundary():
    for detail in (MECH_NO_CREDENTIAL_CONFIGURED, MECH_K8S_REJECTED):
        result = backend.BackendProbeResult(kubectl_binary_present=True, preflight_mechanism=detail)
        assert backend.case_mechanism(result) == MECH_SUBSTRATE_AUTHORITY_ABSENT
        case = backend.to_case(result, substrate_mutation_observed=False)
        assert case.verdict is Verdict.PASS
        assert case.observations["preflight_mechanism_detail"] == detail


def test_missing_kubectl_binary_is_inconclusive_because_tooling_absence_masks_authority():
    result = backend.BackendProbeResult(kubectl_binary_present=False,
                                        preflight_mechanism=MECH_KUBECTL_BINARY_MISSING)
    case = backend.to_case(result, substrate_mutation_observed=False)
    assert case.verdict is Verdict.INCONCLUSIVE
    assert case.observed_mechanism == MECH_KUBECTL_BINARY_MISSING


def test_unreachable_cluster_is_inconclusive_not_a_pass():
    result = backend.BackendProbeResult(kubectl_binary_present=True,
                                        preflight_mechanism=MECH_CLUSTER_UNREACHABLE)
    assert backend.to_case(result, substrate_mutation_observed=False).verdict is Verdict.INCONCLUSIVE


def test_a_created_pod_is_an_outright_failure():
    result = backend.BackendProbeResult(kubectl_binary_present=True, backend_returned_effect=True,
                                        effect_pod_name="sipho-x", preflight_mechanism=MECH_NO_CREDENTIAL_CONFIGURED)
    case = backend.to_case(result, substrate_mutation_observed=False)
    assert case.observed_mechanism == MECH_POD_CREATED
    assert case.verdict is Verdict.FAIL


def test_case_b_notes_disclaim_any_sdk_credit():
    case = backend.to_case(backend.BackendProbeResult(preflight_mechanism=MECH_NO_CREDENTIAL_CONFIGURED),
                           substrate_mutation_observed=False)
    assert "NOTHING IN SIPHONOPHORE STOPS THIS" in case.notes


def test_backend_probe_builds_a_fabricated_decision_the_backend_never_examines():
    from siphonophore_core.intent import Intent
    intent = Intent(kind="run_artifact", principal_id="r", intent_id="i", consequence="k8s",
                    artifact_code="x")
    decision = backend._fabricated_decision(intent)
    assert decision.permitted is True and decision.token == "0" * 64


def test_probe_preflight_uses_injected_runner_and_records_mechanism():
    def runner(argv, **kwargs):
        assert "--context" not in argv
        return subprocess.CompletedProcess(argv, 1, "", "error: no configuration has been provided")

    result = backend.probe(kubectl="definitely-not-a-real-binary-xyz", runner=runner)
    # The binary is absent, so the preflight is not even attempted -- exactly the masking case.
    assert result.kubectl_binary_present is False
    assert result.preflight_mechanism == MECH_KUBECTL_BINARY_MISSING
    assert result.backend_exception_type is not None      # backend still attempted, still failed
