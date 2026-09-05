"""K-live evidence: does the target workload exist?

Pre-registration mapping: criteria 2, 3 and 5, and the FAIL condition "any denied or bypass attempt
mutates Kubernetes state at all".

Queried by the OBSERVER's identity with the OBSERVER's own cluster access -- never by R (which has
no authority to look) and never by reading `Effect.detail` (which would be self-report standing in
for independent observation). This is the module that supplies `substrate_mutation_observed`, the
field `evidence.verdict_for()` refuses to let a case PASS without.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from siphonophore_core.execution_k8s import INTENT_ID_LABEL, MANAGED_BY_LABEL, MANAGED_BY_VALUE


class ObserverUnavailable(RuntimeError):
    """The observer cannot query the cluster. INCONCLUSIVE, never a pass."""


@dataclass(frozen=True)
class ObserverAccess:
    """The observer's own cluster access. Distinct from M's, and distinct from R's absence of one."""

    kubectl: str = "kubectl"
    kubeconfig: str | None = None
    context: str | None = None
    namespace: str = "default"
    timeout: float = 30.0

    def argv(self, args: list[str]) -> list[str]:
        cmd = [self.kubectl, "-n", self.namespace]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        if self.context:
            cmd += ["--context", self.context]
        return cmd + args


@dataclass
class LiveQuery:
    args: list[str]
    returncode: int | None = None
    stdout_json: Any = None
    stderr_bytes: int = 0
    error: str | None = None
    observations: dict[str, Any] = field(default_factory=dict)


def _run(access: ObserverAccess, args: list[str],
         runner: Callable[..., subprocess.CompletedProcess] | None = None) -> LiveQuery:
    argv = access.argv(args)
    query = LiveQuery(args=argv)
    run = runner or subprocess.run
    try:
        proc = run(argv, capture_output=True, text=True, timeout=access.timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        query.error = type(exc).__name__
        return query
    query.returncode = proc.returncode
    query.stderr_bytes = len(proc.stderr or "")
    if proc.returncode == 0 and proc.stdout:
        try:
            query.stdout_json = json.loads(proc.stdout)
        except ValueError:
            query.error = "stdout was not JSON"
    return query


def pod_exists(access: ObserverAccess, pod_name: str, *, runner=None) -> tuple[bool | None, LiveQuery]:
    """`True`/`False` if the answer is known, `None` if the observer could not determine it.

    `None` matters: `verdict_for()` treats unknown substrate state as INCONCLUSIVE, so a failed
    observer query can never be laundered into a passing absence claim."""
    query = _run(access, ["get", "pod", pod_name, "-o", "json"], runner=runner)
    if query.error is not None:
        return None, query
    if query.returncode == 0:
        return True, query
    # kubectl exits non-zero both for NotFound and for "cannot reach the cluster". Distinguish by
    # asking whether the observer can see the namespace at all -- if it can, non-zero means absent.
    probe = _run(access, ["get", "namespace", access.namespace, "-o", "name"], runner=runner)
    query.observations["namespace_probe_returncode"] = probe.returncode
    if probe.returncode == 0:
        return False, query
    return None, query


def list_managed_pods(access: ObserverAccess, *, intent_label: str | None = None, runner=None) -> LiveQuery:
    """Siphonophore-managed Pods, by the backend's own labels (execution_k8s.py:41-43) -- reused
    rather than re-typed so the selector cannot drift from what the backend actually writes."""
    selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
    if intent_label:
        selector += f",{INTENT_ID_LABEL}={intent_label}"
    return _run(access, ["get", "pods", "-l", selector, "-o", "json"], runner=runner)


def count_pods(access: ObserverAccess, *, runner=None) -> tuple[int | None, LiveQuery]:
    """Total Pod count in the namespace -- the before/after invariant that catches a bypass attempt
    creating something the experiment did not think to name."""
    query = _run(access, ["get", "pods", "-o", "json"], runner=runner)
    if query.returncode != 0 or not isinstance(query.stdout_json, dict):
        return None, query
    items = query.stdout_json.get("items")
    return (len(items) if isinstance(items, list) else None), query


def measured_mediator_principal(access: ObserverAccess, *, runner=None) -> tuple[str | None, LiveQuery]:
    """Ask the API server which principal a given kubeconfig actually authenticates as, via
    `kubectl auth whoami`. This is how PROVISIONING_SPEC.md's "the expected M principal must be
    measured during provisioning/preflight and recorded" is satisfied, instead of assuming
    `kubernetes-admin` as Stage 2's topology happened to produce.

    Older kubectl versions lack `auth whoami`; a `None` here means the value must be established
    some other way at preflight, and the run is INCONCLUSIVE for criterion 5 until it is."""
    query = _run(access, ["auth", "whoami", "-o", "json"], runner=runner)
    if query.returncode != 0 or not isinstance(query.stdout_json, dict):
        return None, query
    status = query.stdout_json.get("status") or {}
    user = status.get("userInfo") or {}
    name = user.get("username")
    return (name if isinstance(name, str) else None), query
