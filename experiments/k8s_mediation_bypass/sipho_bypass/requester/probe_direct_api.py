"""Bypass case A / falsification case F-06 and F-13: R addresses the Kubernetes API directly.

Pre-registration mapping: criterion 2 ("R's direct API bypass attempt is rejected by the API
server, and no target Pod exists afterward").

THE DISTINCTION THIS MODULE EXISTS TO PRESERVE. A connection failure is NOT Kubernetes
authorization evidence. "R could not find the API server" and "Kubernetes denied R" are different
facts, and only the second one supports criterion 2. The classifier below keeps them apart and maps
every not-actually-authorization outcome to an INCONCLUSIVE mechanism, never to the expected
boundary.

Making that distinction obtainable requires R to hold NON-SECRET connection material -- the API
server address and the cluster CA certificate -- while holding no client certificate, no bearer
token and no kubeconfig authentication material. That is recorded as PRE-EXECUTION IMPLEMENTATION
CLARIFICATION 1 in README.md: it grants cluster LOCATION and a TRUST ANCHOR, not substrate
execution authority, and it makes R strictly MORE capable, so it cannot weaken the claim.

Raw HTTPS via the standard library, deliberately, rather than `kubectl`: it needs no tooling to be
installed for R, it yields the API server's own status code and `Status` object directly, and it
removes any question of a kubeconfig being silently consulted.
"""
from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error as urlerror, request as urlrequest

from ..evidence import (
    MECH_CONNECT_FAILED, MECH_K8S_ACCEPTED, MECH_K8S_AUTHN_REJECTED, MECH_K8S_AUTHZ_REJECTED,
    MECH_K8S_OTHER_STATUS, MECH_K8S_REJECTED, MECH_NO_CONNECTION_INFO, MECH_TLS_VERIFY_FAILED,
    Category, CaseResult, build_case,
)

# Any 2xx is acceptance. Kept explicit so "201 Created" cannot slip past a `== 200` check.
_ACCEPT_RANGE = range(200, 300)


@dataclass(frozen=True)
class ConnectionInfo:
    """Non-secret cluster location and trust anchor. Contains no authentication material, and the
    provisioning spec forbids placing any here."""

    api_server: str                      # e.g. "https://127.0.0.1:6443"
    ca_cert_path: str | None = None      # cluster CA; a public trust anchor, not a credential
    verify_tls: bool = True


@dataclass
class ApiAttempt:
    name: str
    method: str
    path: str
    status: int | None = None
    mechanism: str = MECH_NO_CONNECTION_INFO
    k8s_reason: str | None = None
    k8s_status_message_present: bool = False
    transport_error: str | None = None
    observations: dict[str, Any] = field(default_factory=dict)


def classify(*, status: int | None, transport_error_kind: str | None) -> str:
    """Pure classifier. Unit-tested against every branch with no network.

    Note the deliberate asymmetry: only 401 and 403 are treated as the expected boundary. A 404 or
    a 500 tells us the request reached the server but says nothing about R's authority, so it is
    `k8s_other_status` -- inconclusive, not a pass."""
    if transport_error_kind == "tls":
        return MECH_TLS_VERIFY_FAILED
    if transport_error_kind == "connect":
        return MECH_CONNECT_FAILED
    if transport_error_kind == "no_connection_info":
        return MECH_NO_CONNECTION_INFO
    if status is None:
        return MECH_CONNECT_FAILED
    if status in _ACCEPT_RANGE:
        return MECH_K8S_ACCEPTED
    if status == 401:
        return MECH_K8S_AUTHN_REJECTED
    if status == 403:
        return MECH_K8S_AUTHZ_REJECTED
    return MECH_K8S_OTHER_STATUS


def _ssl_context(info: ConnectionInfo) -> ssl.SSLContext:
    if not info.verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=info.ca_cert_path)


def _default_transport(info: ConnectionInfo, method: str, path: str, body: bytes | None, timeout: float):
    url = info.api_server.rstrip("/") + path
    req = urlrequest.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    # No Authorization header, ever. R has no bearer token under the threat model, and adding one
    # here would silently change what the experiment measures.
    return urlrequest.urlopen(req, timeout=timeout, context=_ssl_context(info))


def attempt(
    info: ConnectionInfo | None, *, name: str, method: str, path: str,
    body: dict[str, Any] | None = None, timeout: float = 10.0,
    transport: Callable[..., Any] | None = None,
) -> ApiAttempt:
    """One unauthenticated API request, classified. Never raises."""
    result = ApiAttempt(name=name, method=method, path=path)
    if info is None:
        result.mechanism = classify(status=None, transport_error_kind="no_connection_info")
        result.transport_error = "no connection info configured"
        return result

    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    send = transport or _default_transport
    try:
        with send(info, method, path, encoded, timeout) as handle:
            result.status = getattr(handle, "status", None) or handle.getcode()
            raw = handle.read(4096)
        result.observations["response_bytes"] = len(raw)
    except urlerror.HTTPError as exc:
        result.status = exc.code
        try:
            payload = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                # `reason` is a Kubernetes enum ("Unauthorized", "Forbidden"); `message` can embed
                # the requesting identity, so only its PRESENCE is recorded, never its text.
                reason = payload.get("reason")
                result.k8s_reason = reason if isinstance(reason, str) else None
                result.k8s_status_message_present = "message" in payload
        except (ValueError, OSError):
            pass
    except ssl.SSLError as exc:
        result.transport_error = f"ssl:{type(exc).__name__}"
        result.mechanism = classify(status=None, transport_error_kind="tls")
        return result
    except urlerror.URLError as exc:
        kind = "tls" if isinstance(getattr(exc, "reason", None), ssl.SSLError) else "connect"
        result.transport_error = f"urlerror:{type(getattr(exc, 'reason', exc)).__name__}"
        result.mechanism = classify(status=None, transport_error_kind=kind)
        return result
    except (OSError, ValueError) as exc:
        result.transport_error = f"transport:{type(exc).__name__}"
        result.mechanism = classify(status=None, transport_error_kind="connect")
        return result

    result.mechanism = classify(status=result.status, transport_error_kind=None)
    return result


def target_pod_manifest(pod_name: str, namespace: str, image: str = "python:3.12-slim") -> dict[str, Any]:
    """A manifest of the same SHAPE the mediated path produces. Deliberately NOT imported from
    `K8sPodBackend`: R is modelled as an independent attacker writing its own manifest, and reusing
    Siphonophore's builder would make the probe depend on the very component it is bypassing."""
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod_name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "bypass-probe"}},
        "spec": {"restartPolicy": "Never",
                 "containers": [{"name": "artifact", "image": image, "command": ["python", "-c", "pass"]}]},
    }


def run_all(
    info: ConnectionInfo | None, *, pod_name: str, namespace: str = "default",
    alt_namespace: str = "kube-public", transport: Callable[..., Any] | None = None,
) -> list[ApiAttempt]:
    """The enumerated direct-API surface. `create_pod` is the one criterion 2 turns on; the others
    bound falsification case F-13 (alternate namespace, alternate resource kind) and give a read/
    write contrast that distinguishes authn from authz."""
    pod = target_pod_manifest(pod_name, namespace)
    job = {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": pod_name, "namespace": namespace},
        "spec": {"template": {"spec": {"restartPolicy": "Never", "containers": [
            {"name": "artifact", "image": "python:3.12-slim", "command": ["python", "-c", "pass"]}]}}},
    }
    return [
        attempt(info, name="list_pods", method="GET",
                path=f"/api/v1/namespaces/{namespace}/pods", transport=transport),
        attempt(info, name="create_pod", method="POST",
                path=f"/api/v1/namespaces/{namespace}/pods", body=pod, transport=transport),
        attempt(info, name="create_pod_alt_namespace", method="POST",
                path=f"/api/v1/namespaces/{alt_namespace}/pods",
                body=target_pod_manifest(pod_name, alt_namespace), transport=transport),
        attempt(info, name="create_job", method="POST",
                path=f"/apis/batch/v1/namespaces/{namespace}/jobs", body=job, transport=transport),
    ]


def case_mechanism(mechanisms: list[str]) -> str:
    """Pure roll-up, unit-tested independently of any network.

    Takes the WEAKEST outcome, not the best one: if any enumerated attempt merely failed to
    connect, the case is inconclusive even though another attempt was cleanly rejected, because
    criterion 9's enumeration is only as strong as its weakest tried path."""
    if MECH_K8S_ACCEPTED in mechanisms:
        return MECH_K8S_ACCEPTED
    if not mechanisms:
        return MECH_NO_CONNECTION_INFO
    rejected = (MECH_K8S_AUTHN_REJECTED, MECH_K8S_AUTHZ_REJECTED)
    weaker = [m for m in mechanisms if m not in rejected]
    if weaker:
        return weaker[0]
    return MECH_K8S_REJECTED


def to_case(attempts: list[ApiAttempt], *, substrate_mutation_observed: bool | None) -> CaseResult:
    """Collapse the enumerated attempts into bypass case A.

    `expected_boundary` is a CONSTANT here. It is not derived from what was observed -- deriving it
    would make the case pass by construction whenever anything happened to go wrong."""
    return build_case(
        case_id="A-direct-kubernetes-api",
        description="R addresses the Kubernetes API server directly, with no authentication material",
        attempted_path="raw HTTPS from R's identity to the API server (no kubectl, no kubeconfig)",
        expected_boundary=MECH_K8S_REJECTED,
        observed_mechanism=case_mechanism([a.mechanism for a in attempts]),
        substrate_mutation_observed=substrate_mutation_observed,
        evidence_categories=(Category.K_AUTHZ,),
        observations={"attempts": [a.__dict__ for a in attempts]},
        notes=(
            "A connect/TLS failure is NOT Kubernetes authorization evidence and is classified "
            "INCONCLUSIVE. Only 401/403 supports criterion 2. Enumerated paths cover the target "
            "namespace, an alternate namespace and an alternate resource kind (F-13)."
        ),
    )
