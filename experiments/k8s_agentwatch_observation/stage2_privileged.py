"""Stage 2's ONLY point of contact with elevated privilege -- thin wrappers around the
externally-provisioned, fixed-shape root helpers. Nothing here decides what those helpers do; a
separate, already-completed privileged provisioning stage installed them (root-owned, mode 0711,
outside this repo) and granted sipho-agent exactly five NOPASSWD sudo invocations (confirmed via
`sudo -n -l` before this module was written):

    sudo -n /usr/local/libexec/sipho-stage2/cluster ensure
    sudo -n /usr/local/libexec/sipho-stage2/cluster teardown
    sudo -n /usr/local/libexec/sipho-stage2/capture-30  ""
    sudo -n /usr/local/libexec/sipho-stage2/capture-60  ""
    sudo -n /usr/local/libexec/sipho-stage2/capture-120 ""

This module hardcodes exactly those five invocations and nothing else -- no other argv shape is
ever constructed here, mirroring `siphonophore-spawn ""`'s own exact-argv-free-invocation
convention (contracts/spawn_helper.md, SH-08) already established elsewhere in this repo. It does
not, and structurally cannot, grant sipho-agent raw Docker/kind/kubectl/bpftrace/root -- confirmed
directly: `docker ps`, `kind get clusters`, and `nft list ruleset` all fail with a permission error
under this identity (see the Stage 2 design report), and this module never attempts to work around
that; if the fixed shapes below are insufficient for something the experiment needs, the correct
response is to stop and report the gap, not to invent a sixth invocation.

The eBPF helper's own probe was independently confirmed byte-identical to the pinned AgentWatch
checkout's live BPFTRACE_PROGRAM (92037e9ee926ce817829d34923b914b93c16f152) before this module was
written -- see the Stage 2 execution report.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import IO

CLUSTER_HELPER = "/usr/local/libexec/sipho-stage2/cluster"
CAPTURE_HELPERS = {
    30: "/usr/local/libexec/sipho-stage2/capture-30",
    60: "/usr/local/libexec/sipho-stage2/capture-60",
    120: "/usr/local/libexec/sipho-stage2/capture-120",
}

WORKER_KUBECONFIG = Path("/etc/sipho-stage2/sipho-agent.kubeconfig")

# The externally-provisioned cluster reuses Stage 1's own cluster identity (confirmed empirically:
# `cluster ensure`'s own stdout says `Creating cluster "sipho-agentwatch-audit"`) -- same name,
# same derived kubectl context, deliberately not a third cluster (avoids the nondeterministic new
# Docker-bridge-subnet/firewall risk the Stage 2 design report flagged).
CLUSTER_NAME = "sipho-agentwatch-audit"
KUBE_CONTEXT = f"kind-{CLUSTER_NAME}"

# The cluster helper renders its kind config against the root-owned INSTALLED tree at
# /opt/siphonophore (confirmed by reading its world-readable `.rendered-kind-config.yaml` after
# running `ensure`), not this checkout -- so the audit log it actually writes lives there, not
# under this repo's own (gitignored, empty) kind/audit-logs/. Content-diffed identical to this
# checkout's correlate.py/setup_cluster.py/kind-config.yaml.tmpl/audit-policy.yaml before relying
# on this (see the Stage 2 execution report) -- same commit, installed at a fixed, trusted path.
INSTALLED_AUDIT_LOG_PATH = Path(
    "/opt/siphonophore/experiments/k8s_agentwatch_observation/kind/audit-logs/audit.log"
)


class PrivilegedHelperError(RuntimeError):
    """A provisioned sudo invocation itself failed (nonzero, unexpected rc) -- distinct from a
    Siphonophore- or AgentWatch-level failure, which is never routed through this exception."""


def ensure_cluster(timeout: float = 180.0) -> str:
    """`sudo -n cluster ensure` -- idempotent create-if-absent of the fixed, pinned cluster.
    Returns the raw stdout+stderr for evidence preservation."""
    proc = subprocess.run(
        ["sudo", "-n", CLUSTER_HELPER, "ensure"],
        capture_output=True, text=True, timeout=timeout,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise PrivilegedHelperError(f"cluster ensure failed (rc={proc.returncode}): {output[-2000:]}")
    return output


def teardown_cluster(timeout: float = 120.0) -> str:
    """`sudo -n cluster teardown`. Never called automatically mid-experiment -- only at the very
    end of the whole Stage 2 orchestration (after both the ALLOW attempt and the DENY reruns),
    matching this repo's own established "teardown is explicit and separate" convention
    (teardown_cluster.py's own docstring)."""
    proc = subprocess.run(
        ["sudo", "-n", CLUSTER_HELPER, "teardown"],
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.stdout + proc.stderr


class EbpfCapture:
    """One bounded, fixed-duration capture via `sudo -n capture-<N> ""`. Stdout/stderr are
    redirected straight to caller-owned files (never `subprocess.PIPE`) -- bpftrace's own output
    over a 30-120s window on a busy host was already observed (topology-probe evidence) to reach
    tens of KB, comfortably past a pipe's default kernel buffer if a reader ever stalled; writing
    to real files removes the deadlock risk entirely rather than managing it with threads.

    rc 124 (SIGTERM from the helper's own internal `timeout <N>`) is the EXPECTED terminal state of
    a full-duration capture, not a failure -- identical reasoning to
    agentwatch.groundtruth.ebpf_capture.run_capture's own documented rc-124 handling."""

    def __init__(self, duration_s: int, stdout_path: Path, stderr_path: Path) -> None:
        if duration_s not in CAPTURE_HELPERS:
            raise ValueError(f"duration_s must be one of {sorted(CAPTURE_HELPERS)}, got {duration_s}")
        self.duration_s = duration_s
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self._proc: subprocess.Popen | None = None
        self._stdout_fh: IO[bytes] | None = None
        self._stderr_fh: IO[bytes] | None = None
        self.started_monotonic: float | None = None

    def start(self) -> None:
        self._stdout_fh = open(self.stdout_path, "wb")
        self._stderr_fh = open(self.stderr_path, "wb")
        # The sudoers grant is `capture-<N> ""` -- per sudoers argument-matching semantics (see
        # scripts/siphonophore-sudoers.template's own note on the identical `siphonophore-spawn ""`
        # shape), a trailing `""` in the GRANT means "may be invoked with NO arguments," not "may be
        # invoked with one empty-string argument." Empirically confirmed here (not assumed):
        # `sudo -n capture-30 ""` was refused ("sudo: a password is required"); `sudo -n capture-30`
        # (no trailing arg at all) was accepted.
        helper = CAPTURE_HELPERS[self.duration_s]
        self._proc = subprocess.Popen(
            ["sudo", "-n", helper],
            stdout=self._stdout_fh, stderr=self._stderr_fh,
        )
        self.started_monotonic = time.monotonic()

    def wait_attached(self, timeout: float = 10.0, poll_interval: float = 0.1) -> bool:
        """Poll the growing stdout file for bpftrace's own "Attaching N probes..." banner --
        established from ACTUAL startup output, not assumed merely because Popen returned (a
        `sudo`+`timeout`+`bpftrace` process chain starting does not mean the eBPF program has
        actually loaded and attached yet). Returns False on timeout -- the caller must not proceed
        to dispatch as though attachment were confirmed if this returns False."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                text = self.stdout_path.read_text(errors="replace")
            except FileNotFoundError:
                text = ""
            if "Attaching" in text and "probe" in text:
                return True
            if self._proc is not None and self._proc.poll() is not None:
                return False  # exited before ever attaching -- a real failure, not a slow start
            time.sleep(poll_interval)
        return False

    def wait_finished(self, timeout: float | None = None) -> int:
        """Block until the bounded capture's own internal `timeout <N>` ends it (rc 124 expected)
        or it exits on its own. Never signals the child -- sipho-agent (non-root) generally cannot
        send a signal to a process now running as root anyway, and the design explicitly declines
        to request broader authority merely to shorten this wait."""
        assert self._proc is not None
        rc = self._proc.wait(timeout=timeout)
        if self._stdout_fh:
            self._stdout_fh.close()
        if self._stderr_fh:
            self._stderr_fh.close()
        return rc
