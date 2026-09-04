"""Experiment-only glue between Siphonophore and AgentWatch. Lives HERE, not inside either
project's own package: AgentWatch is not a Siphonophore dependency (no entry in pyproject.toml, no
import anywhere under siphonophore_core/ or siphonophore_harness/), and this file does not touch
AgentWatch's own repo either. It reaches AgentWatch's code the same way any other consumer on this
machine would -- a sibling checkout on disk, added to sys.path at import time, configurable via the
AGENTWATCH_REPO env var so this isn't hardcoded to one machine's layout.

The ONLY AgentWatch import here is agentwatch.groundtruth.k8s_audit.parse_lines -- the low-level,
standalone audit-log parser (confirmed by reading its source directly: it takes raw JSON lines and
returns normalized events, no Warrant/GrantEvent/subject_id anywhere in its call graph). Nothing
from agentwatch.reconciler (IdentityCorrelator, k8s_scope) is imported or used -- those modules are
built around a stated demo convention ("K8s ServiceAccount name IS the Warrant subject_id") that
does not apply here and this experiment does not manufacture.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Callable, Iterable

AGENTWATCH_REPO = Path(os.environ.get("AGENTWATCH_REPO", os.path.expanduser("~/dev/agentwatch"))).resolve()
if str(AGENTWATCH_REPO) not in sys.path:
    sys.path.insert(0, str(AGENTWATCH_REPO))

from agentwatch.events import GroundTruthEvent  # noqa: E402 -- import must follow sys.path setup above
from agentwatch.groundtruth.k8s_audit import parse_lines  # noqa: E402

AUDIT_LOG_PATH = Path(__file__).resolve().parent / "kind" / "audit-logs" / "audit.log"


def read_audit_events() -> list[GroundTruthEvent]:
    """Every ResponseComplete-stage `pods` audit event currently in the log, parsed by
    AgentWatch's own, unmodified `parse_lines()` -- the sole point of contact with AgentWatch's
    code anywhere in this experiment. This is Kubernetes AUDIT evidence (an API-server-level
    record of a request and its outcome) -- NOT proof a container process actually ran; see
    README.md's evidentiary-categories section."""
    lines = AUDIT_LOG_PATH.read_text().splitlines()
    events, _stats = parse_lines(lines)
    return events


def wait_for_audit_events(
    predicate: Callable[[GroundTruthEvent], bool], timeout: float = 20.0, poll_interval: float = 0.5,
) -> list[GroundTruthEvent]:
    """Poll the audit log until at least one event matching `predicate` appears, or `timeout`
    elapses. Guards against a real temporal race: nothing guarantees the API server's audit-log
    write is visible to a reader the instant a kubectl call returns."""
    deadline = time.monotonic() + timeout
    matches: list[GroundTruthEvent] = []
    while time.monotonic() < deadline:
        matches = [e for e in read_audit_events() if predicate(e)]
        if matches:
            return matches
        time.sleep(poll_interval)
    return matches


def pods_create_success_events(events: Iterable[GroundTruthEvent], namespace: str, name: str) -> list[GroundTruthEvent]:
    """Independently-parsed audit events for a `pods` CREATE against `namespace/name` that the API
    server accepted (`success is True`, i.e. response code < 400). `args` is `(verb, resource_id)`;
    `resource_id` is built by k8s_audit.py's own `_resource_id()` exactly as
    `f"{resource}:{namespace}/{name}"` for a namespaced object -- reproduced here to match its
    real output, not guessed."""
    resource_id = f"pods:{namespace}/{name}"
    return [e for e in events if e.args == ("create", resource_id) and e.success is True]


def pods_create_events_in_namespace_window(
    events: Iterable[GroundTruthEvent], namespace: str, window_start: float, window_end: float,
) -> list[GroundTruthEvent]:
    """The strongest audit-side absence check available when no specific Pod name exists to check
    against -- true for EVERY DENY case here, not just CognitiveLoop's: k8s_audit.py's parser never
    reads labels (confirmed from source), and a denied dispatch never even reaches pod_name_for()
    (K8sPodBackend.run() is never invoked), so there is no name OR label to filter audit events by
    in either DENY case. This is therefore a windowed, namespace-scoped absence claim -- "did
    anything try to create a Siphonophore-shaped pods object in this namespace during this
    interval" -- not an object-specific one.

    Namespace scoping alone does NOT exclude noise "by construction" -- an earlier version of this
    docstring overclaimed that. It excludes kube-system-sourced bootstrap traffic (kubelet's
    routine `pods get` polling, CoreDNS/kindnet `pods create` at cluster startup -- confirmed
    empirically while standing up this cluster), but NOT arbitrary same-namespace activity: a
    stray manually-created Pod in `default` (confirmed present in this experiment's own audit log
    -- a `smoketest` pod from this cluster's initial smoke test, still in `default`'s audit
    history) would pass a namespace-only filter. Adversarial review caught this. The additional
    filter below -- requiring the object name to carry Siphonophore's own `sipho-` prefix
    (`pod_name_for()`, execution_k8s.py, whose output charset is `[a-z0-9-]` only, confirmed by
    reading it directly -- there is no path for an unprefixed name to reach this shape) -- is
    available without needing labels, since the audit event's `resource_id` already embeds the raw
    object name. This does NOT make the check name/label-*specific* to one execution (any
    Siphonophore-shaped name still passes) -- it only excludes non-Siphonophore activity, so the
    windowed/non-object-specific character of this check (see module docstring) is unchanged."""
    hits = []
    for e in events:
        verb, resource_id = e.args
        if verb != "create":
            continue
        prefix = f"pods:{namespace}/"
        if not resource_id.startswith(prefix):
            continue
        if not resource_id[len(prefix):].startswith("sipho-"):
            continue
        if window_start <= e.ts <= window_end:
            hits.append(e)
    return hits
