"""Stage 2: dual-observation experiment -- Siphonophore <-> AgentWatch Kubernetes.

EXPERIMENTAL QUESTION (Stage 2 design report, unchanged): for one real Siphonophore-mediated
Kubernetes execution, do independent Kubernetes API-audit evidence AND independent host-kernel
eBPF evidence both resolve to the same concrete Kubernetes workload that Siphonophore's Effect
claims to have executed?

This composes Stage 1 (audit leg, unchanged, reused verbatim by rerunning its own tests) with the
topology probe (eBPF/cgroup leg, unchanged machinery) against a REAL Decision/Effect for the first
time. It does not strengthen the claim into causal proof, universal verification, managed-K8s
validation, adversarial robustness, or authorization-from-AgentWatch -- see the Stage 2 design
report's falsification/limitations sections, restated in README.md after this experiment ran.

EVIDENCE CATEGORIES (kept explicit throughout, never collapsed into each other):
  A. SIPHONOPHORE INTERNAL CLAIM  -- Decision/Effect/backend-invocation-count.
  B. KUBERNETES AUDIT              -- AgentWatch's unmodified k8s_audit.parse_lines(), an
                                       API-server-level fact only.
  C. LIVE KUBERNETES OBJECT        -- independently, separately queried via the worker kubeconfig
                                       (LiveObserver) -- never Effect.detail.
  D. HOST KERNEL eBPF              -- AgentWatch's unmodified ebpf.parse_lines(), a kernel-EXEC
                                       fact only.
  E. DERIVED CORRELATION           -- eBPF cgroup ID -> the live-window cgroup map (snapshotted
                                       while the container was still alive, see stage2_observer.py)
                                       -> AgentWatch's unmodified pod_uid_from_cgroup_path(). Never
                                       described as an independent observation in its own right.

Privileged surface used, and ONLY this surface (see stage2_privileged.py):
    sudo -n /usr/local/libexec/sipho-stage2/cluster {ensure,teardown}
    sudo -n /usr/local/libexec/sipho-stage2/capture-120 ""
No raw Docker/kind/kubectl/bpftrace/root access is used or requested anywhere in this file.

Not collected by a bare `pytest` run from the repo root (same convention as Stage 1 --
pyproject.toml's `testpaths` is `tests/` only).
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import correlate
from correlate import wait_for_audit_events
from stage2_correlate import container_id_from_cgroup_path, normalize_container_id
from stage2_observer import LiveObserver
from stage2_privileged import (
    INSTALLED_AUDIT_LOG_PATH,
    KUBE_CONTEXT,
    WORKER_KUBECONFIG,
    EbpfCapture,
)

from siphonophore_core.execution import Executor, ExecutionBackend
from siphonophore_core.execution_k8s import (
    K8sPodBackend,
    ProvisioningError,
    delete_labeled_pods,
    label_value_for,
    require_cluster_reachable,
)
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker

import sys

sys.path.insert(0, "/home/sipho-agent/dev/agentwatch")
from agentwatch.events import EXEC  # noqa: E402
from agentwatch.groundtruth.ebpf import parse_lines as parse_ebpf_lines  # noqa: E402
sys.path.insert(0, "/home/sipho-agent/dev/agentwatch/demo/k8s/ebpf")
from pod_lookup import pod_uid_from_cgroup_path  # noqa: E402

NAMESPACE = "default"
EVIDENCE_ROOT = Path("/tmp/sipho-stage2-evidence")
ALLOW_CAPTURE_DURATION = 120  # see module docstring below the class for why 120, not 60


def _preconditions_met() -> bool:
    try:
        require_cluster_reachable(context=KUBE_CONTEXT, namespace=NAMESPACE)
    except ProvisioningError:
        return False
    return INSTALLED_AUDIT_LOG_PATH.exists()


requires_stage2_fixture = pytest.mark.skipif(
    not _preconditions_met(),
    reason=(
        "needs `sudo -n /usr/local/libexec/sipho-stage2/cluster ensure` run first, and "
        "KUBECONFIG pointed at /etc/sipho-stage2/sipho-agent.kubeconfig"
    ),
)
pytestmark = requires_stage2_fixture


class _CountingK8sPodBackend(ExecutionBackend):
    """Same instrumentation shape as Stage 1's own test file's local class -- not exported there,
    so reproduced here rather than reaching into Stage 1's test module."""

    def __init__(self, real: K8sPodBackend) -> None:
        self._real = real
        self.call_count = 0

    def run(self, decision, intent) -> Effect:
        self.call_count += 1
        return self._real.run(decision, intent)


def _git_head(repo: str) -> str:
    proc = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
    return proc.stdout.strip() or f"<unavailable: {proc.stderr.strip()}>"


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))


MARKER_ARTIFACT_TEMPLATE = (
    "import shutil, os\n"
    "dst = '/tmp/{marker}'\n"
    "shutil.copy('/bin/sleep', dst)\n"
    "os.execv(dst, ['{marker}', '15'])\n"
)


def test_allow_stage2_dual_observation():
    """THE single, pre-registered ALLOW scientific attempt for Stage 2 (Stage 2 design report,
    section J). Not repeated merely to obtain a pass -- if a criterion below fails, that is the
    recorded result, not a reason to retry with a fresh attempt id."""
    attempt_id = f"stage2-allow-{uuid.uuid4().hex[:8]}"
    intent_id = attempt_id
    marker = f"sp2-{uuid.uuid4().hex[:8]}"
    assert len(marker) <= 15, "marker must fit within Linux's comm(7) 15-char limit unambiguously"

    evidence_dir = EVIDENCE_ROOT / attempt_id
    evidence_dir.mkdir(parents=True, exist_ok=False)

    metadata = {
        "attempt_id": attempt_id,
        "intent_id": intent_id,
        "marker": marker,
        "siphonophore_head": _git_head("/home/sipho-agent/siphonophore"),
        "agentwatch_head": _git_head("/home/sipho-agent/dev/agentwatch"),
        "capture_duration_s": ALLOW_CAPTURE_DURATION,
        "started_at": time.time(),
    }
    _write_json(evidence_dir / "00_metadata.json", metadata)

    label_value = label_value_for(intent_id)
    criteria: dict[str, bool] = {}
    notes: dict[str, Any] = {}

    capture = EbpfCapture(
        ALLOW_CAPTURE_DURATION,
        evidence_dir / "bpftrace.stdout",
        evidence_dir / "bpftrace.stderr",
    )
    observer: LiveObserver | None = None
    effect: Effect | None = None
    counting: _CountingK8sPodBackend | None = None

    try:
        # ---- start the eBPF capture BEFORE dispatch, and confirm real attachment ----
        capture.start()
        attached = capture.wait_attached(timeout=10.0)
        notes["capture_attached"] = attached
        if not attached:
            _write_json(evidence_dir / "PRECONDITION_FAILURE.json", {
                "reason": "bpftrace probe did not confirm attachment (no 'Attaching ... probe' "
                          "banner observed) within 10s",
            })
            pytest.fail(
                "environment precondition failure: eBPF capture never confirmed attachment -- "
                "this is not evidence about the experimental hypothesis"
            )

        # ---- start the independent live-object observer BEFORE dispatch ----
        observer = LiveObserver(
            label_value=label_value, namespace=NAMESPACE,
            kubeconfig=WORKER_KUBECONFIG, context=KUBE_CONTEXT,
        )
        observer.start()

        # ---- CATEGORY A: real Siphonophore mediation ----
        gate = Gate(ConsequencePolicy(mapping={"k8s": "k8s_pod"}))
        counting = _CountingK8sPodBackend(K8sPodBackend(context=KUBE_CONTEXT))
        executor = Executor(gate, backends={"k8s_pod": counting})
        broker = Broker(gate=gate, executor=executor)

        artifact_code = MARKER_ARTIFACT_TEMPLATE.format(marker=marker)
        intent = Intent(
            kind="run_artifact", principal_id="agent-a", intent_id=intent_id,
            consequence="k8s", artifact_code=artifact_code,
        )

        dispatch_error: str | None = None
        try:
            effect = broker.dispatch(intent)
        except Exception as exc:  # noqa: BLE001 -- a genuine, reportable ALLOW-side failure,
            # not something to swallow; still worth collecting B/C/D evidence for below, since a
            # Pod may have been created and partially executed before the backend raised.
            dispatch_error = f"{type(exc).__name__}: {exc}"

        if effect is not None:
            category_a = {
                "execution_class": effect.execution_class,
                "backend_invocations": counting.call_count,
                "pod_name": effect.detail.get("pod_name"),
                "phase": effect.detail.get("phase"),
                "exit_code": effect.detail.get("exit_code"),
            }
        else:
            category_a = {
                "execution_class": None, "backend_invocations": counting.call_count,
                "pod_name": None, "phase": None, "exit_code": None,
            }
        _write_json(evidence_dir / "10_category_a_siphonophore_effect.json", {
            **category_a,
            "raw_effect_detail": effect.detail if effect is not None else None,
            "dispatch_error": dispatch_error,
        })
        criteria["1_siphonophore_allow_success"] = (
            effect is not None
            and category_a["execution_class"] == "k8s_pod"
            and category_a["phase"] == "Succeeded"
            and category_a["exit_code"] == 0
            and category_a["backend_invocations"] == 1
        )
        notes["dispatch_error"] = dispatch_error

        # ---- CATEGORY C: independent live Kubernetes object (observer thread) ----
        observer.wait_done(timeout=30.0)
        obs = observer.result
        _write_json(evidence_dir / "20_category_c_live_observation.json", {
            "pod_name": obs.pod_name, "pod_uid": obs.pod_uid, "container_id": obs.container_id,
            "error": obs.error, "candidate_pod_names_seen": obs.candidate_pod_names_seen,
            "timestamps": obs.timestamps,
            "raw_pod_json_at_discovery": obs.raw_pod_json_at_discovery,
            "raw_pod_json_at_running": obs.raw_pod_json_at_running,
        })
        _write_json(evidence_dir / "21_cgroup_map_snapshot.json", obs.cgroup_map_snapshot)
        notes["observer_error"] = obs.error

        # ---- CATEGORY B: independent Kubernetes audit evidence (AgentWatch's own parser) ----
        correlate.AUDIT_LOG_PATH = INSTALLED_AUDIT_LOG_PATH  # see stage2_privileged.py docstring
        pod_name_for_audit = obs.pod_name or category_a["pod_name"]
        audit_events = []
        if pod_name_for_audit:
            audit_events = wait_for_audit_events(
                lambda e: e.args == ("create", f"pods:{NAMESPACE}/{pod_name_for_audit}") and e.success is True,
                timeout=20.0,
            )
        _write_json(evidence_dir / "30_category_b_audit_events.json",
                    [dataclasses.asdict(e) for e in audit_events])
        criteria["2_audit_matches_live_pod_name"] = (
            len(audit_events) == 1
            and obs.pod_name is not None
            and obs.pod_name == category_a["pod_name"]
        )

        # ---- wait for the bounded capture to finish (rc 124 expected, not a failure) ----
        rc = capture.wait_finished(timeout=ALLOW_CAPTURE_DURATION + 30)
        notes["capture_rc"] = rc
        (evidence_dir / "capture_rc.txt").write_text(str(rc))

        # ---- CATEGORY D: independent host kernel eBPF evidence (AgentWatch's own parser) ----
        stdout_lines = (evidence_dir / "bpftrace.stdout").read_text(errors="replace").splitlines()
        ebpf_events, ebpf_stats = parse_ebpf_lines(stdout_lines)
        _write_json(evidence_dir / "40_ebpf_parse_stats.json", dataclasses.asdict(ebpf_stats))

        exec_candidates = [
            e for e in ebpf_events
            if e.kind == EXEC and ((e.exe and marker in e.exe) or e.comm == marker)
        ]
        _write_json(evidence_dir / "41_ebpf_marker_candidates.json",
                    [dataclasses.asdict(e) for e in exec_candidates])
        criteria["3_exactly_one_kernel_exec"] = len(exec_candidates) == 1

        # ---- CATEGORY E: derived correlation, cgroup -> path -> Pod UID / container ID ----
        matched = exec_candidates[0] if len(exec_candidates) == 1 else None
        resolved_pod_uid = None
        resolved_container_id = None
        cgroup_path = None
        if matched is not None and matched.cgroup is not None:
            try:
                cgroup_id_int = int(matched.cgroup)
            except (TypeError, ValueError):
                cgroup_id_int = None
            if cgroup_id_int is not None:
                cgroup_path = obs.cgroup_map_snapshot.get(cgroup_id_int)
            if cgroup_path:
                resolved_pod_uid = pod_uid_from_cgroup_path(cgroup_path)
                resolved_container_id = container_id_from_cgroup_path(cgroup_path)

        _write_json(evidence_dir / "50_category_e_derived_correlation.json", {
            "matched_exec_cgroup_id": matched.cgroup if matched else None,
            "cgroup_path_from_live_window_map": cgroup_path,
            "resolved_pod_uid": resolved_pod_uid,
            "resolved_container_id": resolved_container_id,
        })

        criteria["4_pod_uid_correlation"] = (
            resolved_pod_uid is not None
            and obs.pod_uid is not None
            and resolved_pod_uid == obs.pod_uid
        )

        normalized_live_container_id = normalize_container_id(obs.container_id) if obs.container_id else None
        criteria["5_container_correlation"] = (
            resolved_container_id is not None
            and normalized_live_container_id is not None
            and resolved_container_id == normalized_live_container_id
        )

        criteria["6_three_domain_workload_agreement"] = (
            criteria["2_audit_matches_live_pod_name"]
            and criteria["4_pod_uid_correlation"]
            and criteria["5_container_correlation"]
            and category_a["pod_name"] == obs.pod_name == pod_name_for_audit
        )

        # ---- CATEGORY 7: trust boundary -- checked directly against these new files' own text ----
        this_dir = Path(__file__).resolve().parent
        stage2_files = [
            this_dir / "test_stage2_dual_observation.py",
            this_dir / "stage2_observer.py",
            this_dir / "stage2_privileged.py",
            this_dir / "stage2_correlate.py",
        ]
        # Checked as actual import statements, not bare substring search: a naive substring scan
        # is self-referential against THIS file, since it necessarily names these exact terms to
        # define what's forbidden (found and fixed during the real Stage 2 attempt -- see
        # README.md's "trust-boundary self-check bug" note; the original run's raw false-positive
        # result is preserved unedited in that attempt's evidence directory, not silently erased).
        forbidden_import_re = re.compile(
            r"^\s*(?:import|from)\s+.*\b(agentwatch\.reconciler|IdentityCorrelator|GrantEvent|k8s_scope)\b",
            re.MULTILINE,
        )
        trust_violations = [
            (f.name, m.group(1))
            for f in stage2_files
            for m in forbidden_import_re.finditer(f.read_text())
        ]
        core_diff = subprocess.run(
            ["git", "-C", str(this_dir.parent.parent), "diff", "--stat",
             "siphonophore_core", "siphonophore_harness"],
            capture_output=True, text=True,
        )
        criteria["7_trust_boundary_preserved"] = (
            not trust_violations and core_diff.stdout.strip() == ""
        )
        notes["trust_violations"] = trust_violations
        notes["core_harness_diff"] = core_diff.stdout

        result_summary = {
            "attempt_id": attempt_id,
            "criteria": criteria,
            "all_criteria_pass": all(criteria.values()),
            "notes": notes,
            "finished_at": time.time(),
        }
        _write_json(evidence_dir / "99_result_summary.json", result_summary)

    finally:
        if counting is not None and counting.call_count >= 1:
            # Cleanup runs whenever the backend was actually invoked (a Pod may exist even if
            # dispatch() itself raised) -- and only AFTER all evidence above has already been
            # written to disk, never before (Stage 2 design report's failure/cleanup model).
            delete_labeled_pods(label_value, context=KUBE_CONTEXT)

    # ---- assert LAST, after every artifact above is already safely on disk ----
    failed = {k: v for k, v in criteria.items() if not v}
    assert not failed, f"Stage 2 ALLOW criteria failed: {failed} -- see {evidence_dir}"
