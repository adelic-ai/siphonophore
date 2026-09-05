"""K-audit evidence: WHICH Kubernetes principal created the Pod.

Pre-registration mapping: criterion 5 -- "Independent evidence confirms the mediated Pod was
created, AND the audit record attributes its creation to M's principal, not R's. The attribution
half is the part that is new relative to Stage 2 and is required, not optional."

This is the channel that does genuinely new work in this experiment. In Stage 2 both the requester
and the mediator acted as `kubernetes-admin`, so `user.username` carried no discriminating
information. Under the proposed topology they differ, and that difference is the evidence.

`kubernetes-admin` is deliberately NOT hard-coded anywhere here. The expected mediator principal is
a REQUIRED argument, measured during provisioning/preflight and recorded in the attempt's evidence
(PROVISIONING_SPEC.md). Assuming it would reintroduce exactly the Stage-2 conflation this case
exists to remove.

AgentWatch is reached the same way Stage 1 and Stage 2 reach it -- a sibling checkout added to
`sys.path` at CALL time, via `AGENTWATCH_REPO` -- and the only import is its unmodified
`groundtruth.k8s_audit.parse_lines`. It is not a Siphonophore dependency, it is absent from
`pyproject.toml`, and importing THIS module does not require AgentWatch to be present: the import
is lazy so the cluster-free test suite runs without it.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_AGENTWATCH_REPO = "~/dev/agentwatch"


class AgentWatchUnavailable(RuntimeError):
    """The sibling AgentWatch checkout is not reachable. Always INCONCLUSIVE for the experiment
    ("the audit observer is unavailable before the hypothesis is exercised"), never a pass."""


def load_parse_lines(repo: str | None = None) -> Callable[[list[str]], Any]:
    """Lazily import AgentWatch's own, unmodified audit parser."""
    root = Path(os.path.expanduser(repo or os.environ.get("AGENTWATCH_REPO", DEFAULT_AGENTWATCH_REPO))).resolve()
    if not root.is_dir():
        raise AgentWatchUnavailable(f"AgentWatch checkout not found at {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from agentwatch.groundtruth.k8s_audit import parse_lines  # noqa: PLC0415
    except ImportError as exc:
        raise AgentWatchUnavailable(f"cannot import agentwatch.groundtruth.k8s_audit from {root}") from exc
    return parse_lines


# --- event shape -------------------------------------------------------------------------------
# Stage 1 established the contract this depends on: GroundTruthEvent carries `args == (verb,
# resource_id)` with `resource_id == f"{resource}:{namespace}/{name}"`, plus `success` and `ts`
# (experiments/k8s_agentwatch_observation/correlate.py). The PRINCIPAL field is the one piece Stage
# 1 read but never needed to select on, so its attribute name is treated as an ASSUMPTION here and
# verified at preflight rather than trusted -- see `principal_attribute_report`.
_PRINCIPAL_ATTR_CANDIDATES = ("comm", "username", "user", "principal", "actor")


def principal_of(event: Any) -> str | None:
    """The Kubernetes principal (`user.username`) an audit event is attributed to."""
    for attr in _PRINCIPAL_ATTR_CANDIDATES:
        value = getattr(event, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def principal_attribute_report(sample_event: Any) -> dict[str, Any]:
    """Preflight self-check. MUST be run against a real AgentWatch event before the scientific
    attempt: if none of the candidate attributes carries the principal, criterion 5 cannot be
    evaluated and the run is INCONCLUSIVE rather than silently attributing to `None`."""
    found = {attr: getattr(sample_event, attr, None) for attr in _PRINCIPAL_ATTR_CANDIDATES}
    resolved = principal_of(sample_event)
    return {
        "candidates": {k: (v if isinstance(v, str) else None) for k, v in found.items()},
        "resolved_principal": resolved,
        "usable": resolved is not None,
        "available_attributes": sorted(a for a in dir(sample_event) if not a.startswith("_")),
    }


@dataclass
class CreatorAttribution:
    pod_name: str
    namespace: str
    matching_events: int = 0
    principals: tuple[str, ...] = ()
    attributed_to_mediator: bool | None = None
    attributed_to_requester: bool | None = None
    unattributable: bool = False
    observations: dict[str, Any] = field(default_factory=dict)


def pods_create_events(events: Iterable[Any], *, namespace: str, name: str) -> list[Any]:
    """Successful `pods` CREATE events for one object. `resource_id` is reproduced exactly as
    AgentWatch's own `_resource_id()` builds it, matching Stage 1's verified usage."""
    resource_id = f"pods:{namespace}/{name}"
    return [e for e in events if getattr(e, "args", None) == ("create", resource_id)
            and getattr(e, "success", None) is True]


def attribute_creator(
    events: Iterable[Any], *, namespace: str, pod_name: str,
    expected_mediator_principal: str, requester_principals: tuple[str, ...] = (),
) -> CreatorAttribution:
    """Criterion 5's attribution half. `expected_mediator_principal` is REQUIRED -- there is no
    default, deliberately."""
    if not expected_mediator_principal:
        raise ValueError("expected_mediator_principal must be measured at preflight, never assumed")
    matches = pods_create_events(events, namespace=namespace, name=pod_name)
    principals = tuple(sorted({p for p in (principal_of(e) for e in matches) if p}))
    result = CreatorAttribution(
        pod_name=pod_name, namespace=namespace, matching_events=len(matches), principals=principals,
    )
    if not matches:
        result.observations["reason"] = "no successful pods/create audit event for this object"
        return result
    if not principals:
        result.unattributable = True
        result.observations["reason"] = "audit events found but no principal attribute resolved"
        return result
    result.attributed_to_mediator = principals == (expected_mediator_principal,)
    result.attributed_to_requester = any(p in requester_principals for p in principals)
    result.observations["expected_mediator_principal"] = expected_mediator_principal
    result.observations["requester_principals"] = list(requester_principals)
    return result
