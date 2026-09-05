"""Pure, unprivileged, network-free correlation logic for Stage 2 -- factored out of the
experiment test so it can be unit-tested against canned strings before ever touching a real
cluster or capture (Stage 2 design report's "ordinary/unit tests using canned evidence" step).

Nothing here talks to Kubernetes, the filesystem, or a subprocess. It only interprets strings
already produced elsewhere: a cgroupfs path (from AgentWatch's own
`build_cgroup_id_to_path()`/`/proc/<pid>/cgroup`) and a Kubernetes `containerID` field.
"""
from __future__ import annotations

import re

_CRI_CONTAINERD_RE = re.compile(r"cri-containerd-([0-9a-f]{64})\.scope")
_CONTAINERD_PREFIX = "containerd://"


def container_id_from_cgroup_path(path: str) -> str | None:
    """Extract the bare 64-hex-char containerd container ID from a
    `.../cri-containerd-<id>.scope` cgroupfs path segment -- the same shape independently observed
    in the topology-probe evidence. `None` if the path doesn't contain this segment (e.g. it
    resolved to a higher-level slice, not a leaf container scope)."""
    m = _CRI_CONTAINERD_RE.search(path)
    return m.group(1) if m else None


def normalize_container_id(container_id: str) -> str:
    """Kubernetes reports container IDs as `<runtime>://<id>` (e.g. `containerd://<64 hex>`).
    Strip only the known `containerd://` prefix -- no other normalization, no fuzzy matching
    (Stage 2 falsification rule: container ID comparison must be exact)."""
    if container_id.startswith(_CONTAINERD_PREFIX):
        return container_id[len(_CONTAINERD_PREFIX):]
    return container_id
