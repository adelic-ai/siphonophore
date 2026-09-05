"""LiveObserver -- the concurrent, independent Kubernetes-object watcher that runs alongside
Broker.dispatch() so Stage 2 can inspect the Pod's cgroup WHILE its container is still alive, not
after Effect returns (by which point the container has already exited and its cgroup leaf may be
gone -- see the Stage 2 design report's "cgroup lifetime" section).

This is CATEGORY C (live Kubernetes object) evidence, and it also snapshots the CATEGORY E
(derived correlation) raw material -- the cgroup_id -> path map -- while it is still obtainable.
It never reads `Effect` or anything Siphonophore-internal; it only ever issues its own,
independently-triggered `kubectl` calls against the worker kubeconfig.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/sipho-agent/dev/agentwatch/demo/k8s/ebpf")
from pod_lookup import build_cgroup_id_to_path  # noqa: E402 -- AgentWatch's own, unmodified

from siphonophore_core.execution_k8s import INTENT_ID_LABEL  # noqa: E402 -- reuse, don't duplicate the literal


class AmbiguousPodError(RuntimeError):
    """More than one live Pod carried the attempt's supposedly-unique label -- a correlation
    failure, never silently resolved by picking one (Stage 2 falsification rule)."""


@dataclass
class LiveObservation:
    pod_name: str | None = None
    pod_uid: str | None = None
    container_id: str | None = None
    raw_pod_json_at_discovery: dict | None = None
    raw_pod_json_at_running: dict | None = None
    cgroup_map_snapshot: dict[int, str] = field(default_factory=dict)
    candidate_pod_names_seen: list[str] = field(default_factory=list)
    error: str | None = None
    timestamps: dict[str, float] = field(default_factory=dict)


def _kubectl(args: list[str], kubeconfig: Path, context: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, *args],
        capture_output=True, text=True, timeout=timeout,
    )


class LiveObserver(threading.Thread):
    """Started BEFORE `broker.dispatch()`, so it cannot miss the Pod's Running window. Polls for
    exactly one Pod carrying `label_value` under `siphonophore.dev/intent-id`, then polls that Pod
    until its named artifact container reports `state.running` with a `containerID`, then
    snapshots the whole host cgroup-id->path map (AgentWatch's own, unmodified
    `build_cgroup_id_to_path`) once, immediately -- this is the ONLY point in the whole experiment
    where that snapshot is taken, precisely because it is the last moment the container is known
    still alive."""

    INTENT_ID_LABEL = INTENT_ID_LABEL
    ARTIFACT_CONTAINER_NAME = "artifact"

    def __init__(
        self,
        label_value: str,
        namespace: str,
        kubeconfig: Path,
        context: str,
        discovery_timeout: float = 90.0,
        running_timeout: float = 60.0,
        poll_interval: float = 0.4,
    ) -> None:
        super().__init__(daemon=True)
        self.label_value = label_value
        self.namespace = namespace
        self.kubeconfig = kubeconfig
        self.context = context
        self.discovery_timeout = discovery_timeout
        self.running_timeout = running_timeout
        self.poll_interval = poll_interval
        self.result = LiveObservation()
        self._done = threading.Event()

    def wait_done(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout=timeout)

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 -- record, never crash the observer thread silently
            self.result.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._done.set()

    def _run(self) -> None:
        self.result.timestamps["observer_started"] = time.time()

        # ---- 1/2: discover exactly one Pod carrying this attempt's label ----
        deadline = time.monotonic() + self.discovery_timeout
        pods: list[dict] = []
        while time.monotonic() < deadline:
            proc = _kubectl(
                ["-n", self.namespace, "get", "pods", "-l",
                 f"{self.INTENT_ID_LABEL}={self.label_value}", "-o", "json"],
                self.kubeconfig, self.context,
            )
            if proc.returncode == 0:
                items = json.loads(proc.stdout).get("items", [])
                if items:
                    pods = items
                    break
            time.sleep(self.poll_interval)

        if not pods:
            self.result.error = "no Pod carrying the attempt label appeared within discovery_timeout"
            return

        self.result.candidate_pod_names_seen = [p["metadata"]["name"] for p in pods]
        if len(pods) > 1:
            raise AmbiguousPodError(
                f"expected exactly one Pod for label {self.label_value!r}, found "
                f"{self.result.candidate_pod_names_seen}"
            )

        pod = pods[0]
        self.result.pod_name = pod["metadata"]["name"]
        self.result.pod_uid = pod["metadata"]["uid"]
        self.result.raw_pod_json_at_discovery = pod
        self.result.timestamps["pod_discovered"] = time.time()

        # ---- 3: wait for the artifact container to be Running with a containerID ----
        deadline = time.monotonic() + self.running_timeout
        while time.monotonic() < deadline:
            proc = _kubectl(
                ["-n", self.namespace, "get", "pod", self.result.pod_name, "-o", "json"],
                self.kubeconfig, self.context,
            )
            if proc.returncode == 0:
                pod_obj = json.loads(proc.stdout)
                statuses = pod_obj.get("status", {}).get("containerStatuses", [])
                artifact = next((c for c in statuses if c.get("name") == self.ARTIFACT_CONTAINER_NAME), None)
                if artifact:
                    state = artifact.get("state", {})
                    container_id = artifact.get("containerID")
                    if "running" in state and container_id:
                        self.result.container_id = container_id
                        self.result.raw_pod_json_at_running = pod_obj
                        self.result.timestamps["container_running_observed"] = time.time()
                        # ---- 4: snapshot the cgroup map RIGHT NOW, while still alive ----
                        self.result.cgroup_map_snapshot = build_cgroup_id_to_path()
                        self.result.timestamps["cgroup_map_snapshotted"] = time.time()
                        return
                    if "terminated" in state:
                        # already exited before we caught it Running -- record what we have and
                        # give up on the live-window cgroup snapshot for THIS attempt (a real,
                        # reportable timing gap, not something to paper over).
                        self.result.error = (
                            "artifact container reached terminated state before LiveObserver "
                            "ever observed it Running -- no live-window cgroup snapshot possible"
                        )
                        self.result.raw_pod_json_at_running = pod_obj
                        return
            time.sleep(self.poll_interval)

        self.result.error = "artifact container never reported Running+containerID within running_timeout"
