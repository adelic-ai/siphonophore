"""Bypass case B / falsification case F-07: R invokes `K8sPodBackend` directly.

Pre-registration mapping: criterion 3 -- "R's direct backend bypass creates no target Pod, AND the
experiment records that the enforcing boundary was credential custody, not any Siphonophore check.
The criterion is not satisfied by 'no Pod appeared'; it requires the recorded reason to match the
prediction."

This is the sharpest single result the experiment can produce, and the easiest one to misreport.
Per README.md Finding 3: `K8sPodBackend.run()` performs no authorization check (it reads only
`decision.intent_id`, execution_k8s.py:135), and `Decision` is a plain frozen dataclass anyone can
build. So NOTHING IN SIPHONOPHORE STOPS THIS. If it fails, it fails because R has no Kubernetes
credential -- an OS/Kubernetes fact about the deployment.

CLASSIFICATION, AND WHY IT IS NOT DONE BY READING SIPHONOPHORE'S EXCEPTION TEXT. `ProvisioningError`
and `ExecutionError` embed kubectl's stderr in their message (execution_k8s.py:56-58, 161). Parsing
those strings would make the experiment's verdict depend on Siphonophore's exception wording. So
the probe runs its OWN, independent `kubectl` preflight first, classifies from that command's own
exit status and stderr, and then invokes the backend and records only the exception TYPE. The
string matching that remains is over kubectl's own output -- R's own tooling, which R is entitled
to interpret -- and it is a pure function, unit-tested against canned strings.

THE MASKING PROBLEM, HANDLED EXPLICITLY. If `kubectl` is simply not installed for R, the backend
fails for a TOOLING reason, not an AUTHORITY reason, and the case would pass for the wrong reason.
That is classified as `kubectl_binary_missing` and mapped to INCONCLUSIVE, and PROVISIONING_SPEC.md
requires the kubectl binary to be present for R (a binary is not a credential) precisely so this
case measures what it claims to.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from siphonophore_core.execution_k8s import K8sPodBackend
from siphonophore_core.intent import Intent
from siphonophore_core.policy import Decision

from ..evidence import (
    MECH_CLUSTER_UNREACHABLE, MECH_K8S_REJECTED, MECH_KUBECTL_BINARY_MISSING, MECH_LOCAL_FAILURE,
    MECH_NO_CREDENTIAL_CONFIGURED, MECH_POD_CREATED, MECH_SUBSTRATE_AUTHORITY_ABSENT,
    MECH_TLS_VERIFY_FAILED, Category, CaseResult, build_case,
)

# Markers over KUBECTL's own stderr -- not over any Siphonophore exception message. Ordered: the
# first matching group wins, most specific first.
_STDERR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MECH_NO_CREDENTIAL_CONFIGURED, (
        "no configuration has been provided",
        "invalid configuration",
        "no such file or directory",     # KUBECONFIG points nowhere
        "error loading config file",
        "must be logged in to the server",
        # kubectl's documented no-config fallback: with no kubeconfig at all it targets
        # localhost:8080 and reports a refused connection. That surface looks like
        # "cluster unreachable" but is really "no credential configured", and misreading it
        # would make bypass case B INCONCLUSIVE in exactly the scenario the experiment expects
        # to be in. Matched before the generic connection markers below, deliberately.
        "server localhost:8080 was refused",
        "server 127.0.0.1:8080 was refused",
        "localhost:8080: connect: connection refused",
    )),
    (MECH_K8S_REJECTED, (
        "unauthorized", "forbidden", "cannot create resource", "cannot list resource",
        "error from server (forbidden)",
    )),
    (MECH_TLS_VERIFY_FAILED, (
        "x509", "certificate signed by unknown authority", "tls: ",
    )),
    (MECH_CLUSTER_UNREACHABLE, (
        "connection refused", "no such host", "i/o timeout", "dial tcp",
        "connect: network is unreachable", "the connection to the server",
    )),
)


def classify_kubectl_stderr(*, returncode: int, stderr: str) -> str:
    """Pure classifier over R's own kubectl output. Unit-tested with canned strings."""
    if returncode == 0:
        # kubectl reached the cluster and was allowed to read it. R has usable authority -- the
        # separation premise is broken. Not this function's job to decide the verdict, but this is
        # never the predicted boundary.
        return MECH_LOCAL_FAILURE
    lowered = stderr.lower()
    for mechanism, markers in _STDERR_MARKERS:
        if any(marker in lowered for marker in markers):
            return mechanism
    return MECH_LOCAL_FAILURE


@dataclass
class BackendProbeResult:
    kubectl_binary_present: bool = False
    kubectl_path: str | None = None
    preflight_returncode: int | None = None
    preflight_mechanism: str = MECH_KUBECTL_BINARY_MISSING
    preflight_stderr_bytes: int = 0
    backend_exception_type: str | None = None
    backend_returned_effect: bool = False
    effect_pod_name: str | None = None
    observations: dict[str, Any] = field(default_factory=dict)


def _fabricated_decision(intent: Intent) -> Decision:
    """Everything a `Decision` needs, none of it authorized. `permitted=True` and a nonsense token
    are both fine here: the backend never looks at either (README.md Finding 3), and demonstrating
    that is part of the point."""
    return Decision(
        intent_id=intent.intent_id, principal_id=intent.principal_id, kind=intent.kind,
        permitted=True, execution_class="k8s_pod", artifact_digest="0" * 64, token="0" * 64,
    )


def probe(
    *, namespace: str = "default", kubectl: str = "kubectl", context: str | None = None,
    intent_id: str = "bypass-direct-backend", artifact_code: str = "print('direct-backend-bypass')",
    timeout: float = 30.0, runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> BackendProbeResult:
    """Run the enumerated direct-backend attempt. Never raises."""
    result = BackendProbeResult()

    which = shutil.which(kubectl)
    result.kubectl_binary_present = which is not None
    result.kubectl_path = which
    if which is None:
        result.preflight_mechanism = MECH_KUBECTL_BINARY_MISSING
    else:
        cmd = [kubectl, "-n", namespace]
        if context:
            cmd += ["--context", context]
        cmd += ["get", "namespace", namespace, "-o", "name"]
        run = runner or subprocess.run
        try:
            completed = run(cmd, capture_output=True, text=True, timeout=timeout)
            result.preflight_returncode = completed.returncode
            stderr = completed.stderr or ""
            result.preflight_stderr_bytes = len(stderr)
            result.preflight_mechanism = classify_kubectl_stderr(returncode=completed.returncode, stderr=stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            result.preflight_mechanism = MECH_LOCAL_FAILURE
            result.observations["preflight_exception"] = type(exc).__name__

    # The actual bypass attempt, using the REAL backend with a fabricated Decision.
    intent = Intent(kind="run_artifact", principal_id="bypass-requester", intent_id=intent_id,
                    consequence="k8s", artifact_code=artifact_code)
    backend = K8sPodBackend(namespace=namespace, kubectl=kubectl, context=context, timeout=timeout)
    try:
        effect = backend.run(_fabricated_decision(intent), intent)
    except Exception as exc:  # noqa: BLE001 -- type only; the message embeds mediator-side detail
        result.backend_exception_type = type(exc).__name__
    else:
        result.backend_returned_effect = True
        pod = effect.detail.get("pod_name")
        result.effect_pod_name = pod if isinstance(pod, str) else None
    return result


# The two observable forms of "R has no Kubernetes authority". Both satisfy the prediction; the
# finer distinction is preserved in `observations`, not in the verdict.
_AUTHORITY_ABSENT_FORMS = (MECH_NO_CREDENTIAL_CONFIGURED, MECH_K8S_REJECTED)


def case_mechanism(result: "BackendProbeResult") -> str:
    """Pure roll-up, unit-tested without a cluster."""
    if result.backend_returned_effect:
        return MECH_POD_CREATED
    if result.preflight_mechanism in _AUTHORITY_ABSENT_FORMS:
        return MECH_SUBSTRATE_AUTHORITY_ABSENT
    return result.preflight_mechanism


def to_case(result: BackendProbeResult, *, substrate_mutation_observed: bool | None) -> CaseResult:
    """Criterion 3. `expected_boundary` is a constant, fixed before the run, and it deliberately
    names a DEPLOYMENT fact -- not a Siphonophore check."""
    return build_case(
        case_id="B-direct-backend",
        description="R instantiates the real K8sPodBackend and calls run() with a fabricated Decision",
        attempted_path="K8sPodBackend.run(fabricated Decision, Intent) in R's own process",
        expected_boundary=MECH_SUBSTRATE_AUTHORITY_ABSENT,
        observed_mechanism=case_mechanism(result),
        substrate_mutation_observed=substrate_mutation_observed,
        evidence_categories=(Category.S, Category.O, Category.K_AUTHZ),
        observations={
            "kubectl_binary_present": result.kubectl_binary_present,
            "kubectl_path": result.kubectl_path,
            "preflight_returncode": result.preflight_returncode,
            "preflight_mechanism_detail": result.preflight_mechanism,
            "preflight_stderr_bytes": result.preflight_stderr_bytes,
            "backend_exception_type": result.backend_exception_type,
            "backend_returned_effect": result.backend_returned_effect,
            **result.observations,
        },
        notes=(
            "NOTHING IN SIPHONOPHORE STOPS THIS. K8sPodBackend.run() performs no authorization "
            "check (execution_k8s.py:130-207); the Decision here is fabricated and is never "
            "examined. Any PASS is evidence about the DEPLOYMENT's credential custody and must "
            "never be reported as the SDK having resisted anything. `kubectl_binary_missing` and "
            "`cluster_unreachable` are INCONCLUSIVE: tooling absence or a dead cluster would mask "
            "the authority question rather than answer it."
        ),
    )
