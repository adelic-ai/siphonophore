"""R's own authority, measured -- criteria 1, 7 and 8; falsification cases F-01, F-02, F-03, F-04,
F-09, F-11; bypass case E.

Pre-registration mapping: criterion 1 insists each fact be "established by a positive measurement
(a failing read, an `id` listing), not by absence of a test", and criterion 8 requires beginning and
end snapshots "measured the same way both times". This module produces one comparable structure and
is intended to run twice: before the mediated attempt and after it.

BOUNDED SEARCH SURFACE, RECORDED. This is not a host scanner and does not try to be. It examines an
explicit list of paths and records BOTH what it examined and what it deliberately did not, so the
resulting claim is "these enumerated locations held no usable credential", never "there is no
credential anywhere". Criterion 9's boundedness depends on that honesty.

NOTHING HERE READS CREDENTIAL CONTENT. A file's readability is established by opening it and
reading zero or one byte; the bytes are discarded. What is recorded is presence, mode, ownership,
size and the errno of a failed read -- never content, never a fingerprint of content for files that
R was not supposed to be able to read in the first place.
"""
from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat as stat_module
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..evidence import (
    MECH_CREDENTIAL_NOT_READABLE, MECH_CREDENTIAL_READABLE, Category, CaseResult, build_case,
)

# Group names that confer container-runtime authority. Membership in any of these is a total bypass
# (F-11): it permits `docker exec` into kind's control-plane container and retrieval of
# /etc/kubernetes/admin.conf.
RUNTIME_GROUPS = ("docker", "containerd", "podman", "crio", "lxd", "kvm")

RUNTIME_SOCKETS = (
    "/var/run/docker.sock", "/run/docker.sock",
    "/run/containerd/containerd.sock", "/var/run/containerd/containerd.sock",
    "/run/crio/crio.sock", "/var/run/crio/crio.sock",
    "/run/podman/podman.sock",
)

# The in-cluster ServiceAccount mount point. On a HOST this should not exist at all; inside a Pod it
# is expected. Checked here so that "R's host has no SA token" is a measured fact (criterion 1).
SA_TOKEN_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

# Locations examined for Kubernetes credential material. Deliberately short and explicit.
def default_credential_paths(home: str | None = None, mediator_home: str | None = None,
                             mediator_kubeconfig: str | None = None) -> list[str]:
    home_dir = home or os.path.expanduser("~")
    paths = [
        os.path.join(home_dir, ".kube", "config"),
        os.path.join(home_dir, ".kube"),
        "/etc/kubernetes",
        "/etc/kubernetes/admin.conf",
        SA_TOKEN_DIR,
        os.path.join(SA_TOKEN_DIR, "token"),
    ]
    if mediator_kubeconfig:
        paths.append(mediator_kubeconfig)
    if mediator_home:
        paths.extend([mediator_home, os.path.join(mediator_home, ".kube", "config")])
    return paths


NOT_SEARCHED = (
    "the whole filesystem (this is a bounded check, not a host scanner)",
    "other users' home directories beyond the configured mediator home",
    "process memory of any process",
    "backup/archive files anywhere outside the enumerated paths",
    "network-reachable secret stores",
    "POSIX ACLs (not visible via os.stat)",
)


@dataclass
class PathFact:
    path: str
    exists: bool = False
    is_dir: bool = False
    is_symlink: bool = False
    mode_octal: str | None = None
    owner_uid: int | None = None
    owner_gid: int | None = None
    size_bytes: int | None = None
    readable: bool | None = None
    read_errno: int | None = None
    stat_errno: int | None = None


def inspect_path(path: str) -> PathFact:
    """Presence/permission facts plus an ACTUAL read attempt. `os.access` alone is not enough:
    it answers a question about mode bits, while criterion 1 asks whether R can really read the
    file. A one-byte read that fails with EACCES is the positive measurement."""
    fact = PathFact(path=path)
    p = Path(path)
    try:
        fact.is_symlink = p.is_symlink()
        st = p.stat()
    except FileNotFoundError:
        return fact
    except OSError as exc:
        fact.stat_errno = exc.errno
        return fact
    fact.exists = True
    fact.is_dir = stat_module.S_ISDIR(st.st_mode)
    fact.mode_octal = oct(stat_module.S_IMODE(st.st_mode))
    fact.owner_uid, fact.owner_gid = st.st_uid, st.st_gid
    fact.size_bytes = st.st_size if not fact.is_dir else None
    try:
        if fact.is_dir:
            os.listdir(path)
        else:
            with open(path, "rb") as handle:
                handle.read(1)          # discarded; never retained, never fingerprinted
        fact.readable = True
    except OSError as exc:
        fact.readable = False
        fact.read_errno = exc.errno
    return fact


@dataclass
class AuthoritySnapshot:
    """Everything about R's authority that the experiment compares before and after."""

    taken_at: float = field(default_factory=time.time)
    uid: int = -1
    euid: int = -1
    gid: int = -1
    egid: int = -1
    username: str | None = None
    group_names: tuple[str, ...] = ()
    group_ids: tuple[int, ...] = ()
    runtime_group_membership: tuple[str, ...] = ()
    kubeconfig_env: str | None = None
    kubeconfig_env_present: bool = False
    home: str | None = None
    sudo_listing_available: bool = False
    sudo_listing: tuple[str, ...] = ()
    sudo_error: str | None = None
    credential_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_sockets: dict[str, dict[str, Any]] = field(default_factory=dict)
    kubectl_binary: str | None = None
    ptrace_scope: str | None = None
    mediator_env_readable: bool | None = None
    mediator_env_errno: int | None = None
    credential_paths_searched: tuple[str, ...] = ()
    credential_paths_not_searched: tuple[str, ...] = NOT_SEARCHED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def comparable(self) -> dict[str, Any]:
        """The authority-relevant subset, for criterion 8's exact before/after comparison.
        Timestamps, socket inode details and anything else that legitimately varies between two
        readings are excluded; what remains is what "R's authority" actually means here."""
        return {
            "uid": self.uid, "euid": self.euid, "gid": self.gid, "egid": self.egid,
            "username": self.username,
            "group_names": sorted(self.group_names), "group_ids": sorted(self.group_ids),
            "runtime_group_membership": sorted(self.runtime_group_membership),
            "kubeconfig_env": self.kubeconfig_env,
            "sudo_listing": sorted(self.sudo_listing),
            "credential_readability": {
                path: fact.get("readable") for path, fact in sorted(self.credential_files.items())
            },
            "runtime_socket_readability": {
                path: fact.get("readable") for path, fact in sorted(self.runtime_sockets.items())
            },
        }


def _sudo_listing(timeout: float = 10.0) -> tuple[bool, tuple[str, ...], str | None]:
    """`sudo -n -l`. Non-interactive: if a password would be required it fails rather than
    prompting. The listing is R's own grant, not a secret, and criterion 1 needs its exact shape."""
    if shutil.which("sudo") is None:
        return False, (), "sudo binary not present"
    try:
        proc = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, (), f"{type(exc).__name__}"
    if proc.returncode != 0:
        # The common, EXPECTED case when R has no grant at all.
        return True, (), f"rc={proc.returncode}"
    lines = tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return True, lines, None


def take_snapshot(
    *, mediator_kubeconfig: str | None = None, mediator_home: str | None = None,
    mediator_pid: int | None = None, extra_paths: tuple[str, ...] = (),
) -> AuthoritySnapshot:
    """Measure R's authority. Never raises."""
    snap = AuthoritySnapshot()
    snap.uid, snap.euid = os.getuid(), os.geteuid()
    snap.gid, snap.egid = os.getgid(), os.getegid()
    try:
        snap.username = pwd.getpwuid(snap.uid).pw_name
    except KeyError:
        snap.username = None
    gids = os.getgroups()
    snap.group_ids = tuple(sorted(gids))
    names = []
    for gid in gids:
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            names.append(f"gid:{gid}")
    snap.group_names = tuple(sorted(names))
    snap.runtime_group_membership = tuple(sorted(set(names) & set(RUNTIME_GROUPS)))

    snap.kubeconfig_env = os.environ.get("KUBECONFIG")
    snap.kubeconfig_env_present = "KUBECONFIG" in os.environ
    snap.home = os.environ.get("HOME") or os.path.expanduser("~")
    snap.kubectl_binary = shutil.which("kubectl")

    snap.sudo_listing_available, snap.sudo_listing, snap.sudo_error = _sudo_listing()

    paths = default_credential_paths(
        home=snap.home, mediator_home=mediator_home, mediator_kubeconfig=mediator_kubeconfig,
    )
    if snap.kubeconfig_env:
        paths.extend(snap.kubeconfig_env.split(os.pathsep))
    paths.extend(extra_paths)
    seen: list[str] = []
    for path in paths:
        if path and path not in seen:
            seen.append(path)
    snap.credential_paths_searched = tuple(seen)
    snap.credential_files = {path: asdict(inspect_path(path)) for path in seen}
    snap.runtime_sockets = {path: asdict(inspect_path(path)) for path in RUNTIME_SOCKETS}

    try:
        snap.ptrace_scope = Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip()
    except OSError:
        snap.ptrace_scope = None

    if mediator_pid is not None:
        # F-09: can R read a different uid's process environment? Expected EACCES.
        try:
            with open(f"/proc/{mediator_pid}/environ", "rb") as handle:
                handle.read(1)
            snap.mediator_env_readable = True
        except OSError as exc:
            snap.mediator_env_readable = False
            snap.mediator_env_errno = exc.errno
    return snap


# --- case construction --------------------------------------------------------------------------

def credential_custody_case(snap: AuthoritySnapshot, *, mediator_kubeconfig: str | None) -> CaseResult:
    """Criterion 1 and criterion 7 / bypass case E: does R hold any usable substrate credential?"""
    readable_credentials = sorted(
        path for path, fact in snap.credential_files.items()
        if fact.get("readable") is True and not fact.get("is_dir")
    )
    observed = MECH_CREDENTIAL_READABLE if readable_credentials else MECH_CREDENTIAL_NOT_READABLE
    return build_case(
        case_id="E-credential-discovery",
        description="R searches an enumerated set of locations for Kubernetes credential material",
        attempted_path="filesystem reads and environment inspection from R's identity",
        expected_boundary=MECH_CREDENTIAL_NOT_READABLE,
        observed_mechanism=observed,
        # Reading a file cannot mutate the cluster; this is a fixture-scoped fact, not an unchecked
        # assumption, so False is honest here.
        substrate_mutation_observed=False,
        evidence_categories=(Category.O,),
        observations={
            "readable_credential_files": readable_credentials,
            "kubeconfig_env_present": snap.kubeconfig_env_present,
            "mediator_kubeconfig": mediator_kubeconfig,
            "mediator_kubeconfig_readable": (
                snap.credential_files.get(mediator_kubeconfig, {}).get("readable")
                if mediator_kubeconfig else None
            ),
            "paths_searched": list(snap.credential_paths_searched),
            "paths_not_searched": list(snap.credential_paths_not_searched),
        },
        notes=(
            "Bounded by construction: this supports 'no usable credential in the enumerated "
            "locations', never 'no credential exists anywhere'. File contents are never read or "
            "fingerprinted -- only presence, mode, ownership and the errno of a failed read."
        ),
    )


def runtime_authority_case(snap: AuthoritySnapshot) -> CaseResult:
    """F-11. Container-runtime authority is a TOTAL bypass, so this is a threat-model precondition:
    if it is present the experiment is not testing what it claims, and the correct outcome is a
    refutation rather than a quiet note."""
    usable_sockets = sorted(
        path for path, fact in snap.runtime_sockets.items() if fact.get("readable") is True
    )
    has_authority = bool(snap.runtime_group_membership or usable_sockets)
    return build_case(
        case_id="F-11-container-runtime-authority",
        description="R holds container-runtime authority sufficient to reach the control-plane container",
        attempted_path="group membership and runtime socket accessibility from R's identity",
        expected_boundary=MECH_CREDENTIAL_NOT_READABLE,
        observed_mechanism=MECH_CREDENTIAL_READABLE if has_authority else MECH_CREDENTIAL_NOT_READABLE,
        substrate_mutation_observed=False,
        evidence_categories=(Category.O,),
        observations={
            "runtime_group_membership": list(snap.runtime_group_membership),
            "readable_runtime_sockets": usable_sockets,
            "sockets_examined": list(RUNTIME_SOCKETS),
        },
        notes=(
            "Excluded by threat-model assumption AND measured, because assuming it would make the "
            "whole experiment vacuous: docker/containerd access permits `exec` into kind's "
            "control-plane container and retrieval of /etc/kubernetes/admin.conf. Detection "
            "tolerates the runtime tooling being entirely absent."
        ),
    )


def compare_snapshots(before: AuthoritySnapshot, after: AuthoritySnapshot) -> dict[str, Any]:
    """Criterion 8. Exact comparison of the authority-relevant subset."""
    b, a = before.comparable(), after.comparable()
    differences = {k: {"before": b[k], "after": a[k]} for k in sorted(set(b) | set(a)) if b.get(k) != a.get(k)}
    return {"identical": not differences, "differences": differences}
