"""K8sPodBackend -- the k8s_pod ExecutionBackend (DESIGN.md section 2 / docs/EXECUTION.md).

Runs `intent.artifact_code` as a real Pod on a real Kubernetes cluster (kind, locally; the same
shape should hold against a managed cluster -- see docs/EXECUTION_K8S.md for what's proven and
what isn't yet). Shells out to `kubectl` rather than linking a Kubernetes client library: the
actual cluster-facing operation is a single, auditable external call, the same reason
`uid_cgroup`'s backends shell out to `useradd`/`siphonophore-spawn` instead of calling libc
directly, and it keeps this package's own zero-dependency stance (pyproject.toml) unchanged.

This is the minimal vertical slice, deliberately: one Pod per execution, `restartPolicy: Never`,
waited to completion, logs and exit code collected. No check-in/identity-binding tier exists for
this class yet (unlike `uid_cgroup_checkin`) -- see docs/EXECUTION_K8S.md's follow-on list for why
that's out of scope here rather than a hidden gap.

Deliberately does NOT delete the Pod it creates on the success path -- the same disclosed-not-fixed
shape as `execution_uid_cgroup.py`'s cgroup leaves (DESIGN.md). Here it's also useful, not merely
tolerated: it's what lets an independent observer (a test, or a real external tool such as
AgentWatch) inspect the actual Pod object and its logs after the fact, rather than trusting only
what this backend's own `Effect` claims. `delete_labeled_pods()` below is the explicit, separate
cleanup path -- a test fixture's job, not this backend's.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from typing import Any

from .execution import ExecutionBackend, ExecutionError
from .intent import Effect, Intent
from .policy import Decision


class ProvisioningError(RuntimeError):
    """kubectl itself isn't usable, or can't reach a cluster/namespace -- distinct from a Pod that
    ran but whose artifact failed (ExecutionError, raised by `run()` itself)."""


INTENT_ID_LABEL = "siphonophore.dev/intent-id"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "siphonophore"


def require_cluster_reachable(kubectl: str = "kubectl", context: str | None = None, namespace: str = "default") -> None:
    cmd = [kubectl]
    if context:
        cmd += ["--context", context]
    cmd += ["get", "namespace", namespace, "-o", "name"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except FileNotFoundError as exc:
        raise ProvisioningError(f"{kubectl!r} not found on PATH") from exc
    if result.returncode != 0:
        raise ProvisioningError(
            f"kubectl cannot reach namespace {namespace!r} (rc={result.returncode}): {result.stderr.strip()}"
        )


# Kubernetes label values: [a-z0-9A-Z] at each end, '-'/'_'/'.' allowed in the middle, <=63 chars.
# A Pod name is stricter still (RFC 1123 DNS label: lowercase alphanumeric/'-' only). execution_id
# (decision.intent_id) is caller-supplied and generally won't already satisfy either -- this
# backend derives compliant values from it rather than widening what every other backend already
# accepts as an execution_id (execution_uid_cgroup.py's own charset check is unrelated and
# untouched; each backend validates for its own substrate's naming rules, deliberately not a
# shared core concept -- see docs/EXECUTION_K8S.md).
_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")
_NAME_UNSAFE_RE = re.compile(r"[^a-z0-9-]")


def label_value_for(execution_id: str) -> str:
    """A best-effort, collision-tolerant label value for `execution_id` -- used only for
    after-the-fact correlation (kubectl / an external observer selecting on it), never as the
    Pod's own unique identity (see `pod_name_for`)."""
    safe = _LABEL_UNSAFE_RE.sub("-", execution_id).strip("-.")[:63]
    return safe or "exec"


def pod_name_for(execution_id: str) -> str:
    """A real Kubernetes Pod name must be a unique RFC 1123 DNS label; execution_id has no such
    guarantee (it's shared, unmodified, with every other backend's own naming convention). Slug it
    for readability and append a short random suffix for uniqueness -- unlike
    `execution_uid_cgroup.py`'s cgroup directory name, which fails loudly on collision instead of
    disambiguating, because reusing this backend's execution_id across two live Pods is an ordinary
    occurrence (retries, repeated test runs), not a sign of something wrong."""
    slug = _NAME_UNSAFE_RE.sub("-", execution_id.lower()).strip("-")[:40]
    return f"sipho-{slug or 'exec'}-{uuid.uuid4().hex[:8]}"


_ARTIFACT_WRAPPER = """
import json, sys
payload = json.loads(sys.argv[1])
{body}
"""

_TERMINAL_PHASES = {"Succeeded", "Failed"}


class K8sPodBackend(ExecutionBackend):
    """`ExecutionBackend` for `k8s_pod`. Caller-configurable namespace/image/context so multiple
    call sites don't collide, mirroring `UidCgroupBackend`'s own caller-configurable uid
    range/cgroup root for the identical reason."""

    def __init__(
        self,
        namespace: str = "default",
        image: str = "python:3.12-slim",
        kubectl: str = "kubectl",
        context: str | None = None,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> None:
        self._namespace = namespace
        self._image = image
        self._kubectl = kubectl
        self._context = context
        self._timeout = timeout
        self._poll_interval = poll_interval

    def _kubectl_base(self) -> list[str]:
        cmd = [self._kubectl, "-n", self._namespace]
        if self._context:
            cmd += ["--context", self._context]
        return cmd

    def _run_kubectl(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run([*self._kubectl_base(), *args], capture_output=True, text=True, **kwargs)

    def run(self, decision: Decision, intent: Intent) -> Effect:
        require_cluster_reachable(self._kubectl, self._context, self._namespace)
        if intent.artifact_code is None:
            raise ExecutionError("k8s_pod backend requires intent.artifact_code")

        execution_id = decision.intent_id
        pod_name = pod_name_for(execution_id)
        wrapped = _ARTIFACT_WRAPPER.format(body=intent.artifact_code)

        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "labels": {
                    MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                    INTENT_ID_LABEL: label_value_for(execution_id),
                },
            },
            "spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": "artifact",
                    "image": self._image,
                    "command": ["python", "-c", wrapped, json.dumps(intent.payload)],
                }],
            },
        }

        create = self._run_kubectl(["apply", "-f", "-"], input=json.dumps(manifest))
        if create.returncode != 0:
            raise ExecutionError(f"k8s_pod create failed (rc={create.returncode}): {create.stderr.strip()}")

        deadline = time.monotonic() + self._timeout
        phase = "Pending"
        pod_status: dict = {}
        while time.monotonic() < deadline:
            get = self._run_kubectl(["get", "pod", pod_name, "-o", "json"])
            if get.returncode != 0:
                raise ExecutionError(f"k8s_pod status check failed (rc={get.returncode}): {get.stderr.strip()}")
            pod_status = json.loads(get.stdout)
            phase = pod_status.get("status", {}).get("phase", "Unknown")
            if phase in _TERMINAL_PHASES:
                break
            time.sleep(self._poll_interval)
        else:
            raise ExecutionError(f"k8s_pod {pod_name!r} did not reach a terminal phase within {self._timeout}s (last phase={phase!r})")

        logs = self._run_kubectl(["logs", f"pod/{pod_name}", "--container=artifact"])
        # By container name, not container_statuses[0] -- a cluster with sidecar injection
        # (admission-webhook-added containers, common in real deployments though not exercised by
        # the kind demo) would put an arbitrary container at index 0, silently reading the wrong
        # exit code.
        container_statuses = pod_status.get("status", {}).get("containerStatuses", [])
        artifact_status = next((cs for cs in container_statuses if cs.get("name") == "artifact"), None)
        terminated = (artifact_status or {}).get("state", {}).get("terminated", {})
        exit_code = terminated.get("exitCode")
        node_name = pod_status.get("spec", {}).get("nodeName")

        detail: dict[str, Any] = {
            "pod_name": pod_name,
            "namespace": self._namespace,
            "node_name": node_name,
            "phase": phase,
            "exit_code": exit_code,
            "stdout": logs.stdout,
        }

        # exit_code is REQUIRED to be exactly 0, not merely "not a known failure" -- a Pod that
        # reaches phase Succeeded with no terminated state reported (a containerStatuses shape
        # this backend didn't anticipate) must fail closed, not silently pass on phase alone. This
        # branch is defensive and untested against a real cluster -- normal Pod completion always
        # reports an integer exitCode alongside phase=Succeeded, so it doesn't occur in the kind
        # demo; disclosed here rather than silently claimed as covered.
        if phase != "Succeeded" or exit_code != 0:
            raise ExecutionError(f"k8s_pod {pod_name!r} did not succeed: phase={phase!r} exit_code={exit_code!r} logs={logs.stdout!r}{logs.stderr!r}")

        return Effect(intent_id=intent.intent_id, execution_class="k8s_pod", detail=detail)


def delete_labeled_pods(
    label_value: str,
    namespace: str = "default",
    kubectl: str = "kubectl",
    context: str | None = None,
) -> None:
    """Explicit, separate cleanup for Pods this backend created -- never called by `run()` itself.
    Intended for test/demo teardown. Blocks until the Pod is actually gone (no `--wait=false`) --
    a test elsewhere in the same suite that checks "no new managed Pod appeared" via a total-count
    snapshot (rather than a label-specific query -- some callers lose the correlating intent_id
    when a GateViolation propagates before an Effect exists) depends on a prior test's cleanup
    having actually completed, not merely been requested, by the time it takes its own snapshot."""
    cmd = [kubectl, "-n", namespace]
    if context:
        cmd += ["--context", context]
    cmd += ["delete", "pod", "-l", f"{INTENT_ID_LABEL}={label_value}", "--ignore-not-found"]
    subprocess.run(cmd, capture_output=True, text=True)
