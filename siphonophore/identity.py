"""Real per-node OS identity: an ephemeral uid from a static, pre-reserved range (same shape,
same reasoning, as warrant's `uid_pool.py` -- a session that crashes without clean teardown leaves
an orphaned OS user, not a stale rule, so a static range beats per-node dynamic firewall/ACL
entries), paired with a cgroup for tracking that node's *entire* descendant subtree, not just its
own pid.

Why cgroup, not pid, for the subtree: a pid identifies one process; it says nothing about that
process's own children if a node's own tool later forks or execs further work -- the same
fork-gap blindness exec-only observation (auditd) has always had (see warden's `DECISIONS.md`:
"the capture plane ships fork-gap-blind"). A cgroup v2 directory does not have this gap: cgroup
membership is inherited across fork by the kernel itself, automatically, with no re-parenting step
needed -- a child that forks without ever exec'ing is still a member of its parent's cgroup unless
it explicitly moves itself. Reading `cgroup.procs` on the node's own cgroup therefore returns its
whole live descendant subtree, not just the one pid that was originally spawned.

That "unless it explicitly moves itself" is the actual security property this module has to get
right, not an afterthought: a provisioned node's own uid must NOT have write access to its own
cgroup's control files, or a compromised node could write its own pid out of the cgroup it's
supposed to be trapped in and defeat the whole tracking scheme. `create_cgroup` creates the
directory as whatever uid this process (the broker, expected to run more-privileged than any node
it provisions) is running as -- not the node's uid -- so a node process, running under its own
allocated uid, has no permission to touch `cgroup.procs` for its own cgroup. Provisioning must
always run at higher privilege than what it provisions; see DESIGN.md's privilege-asymmetry point.

Linux-only. Needs root (useradd/userdel, and write access to /sys/fs/cgroup). Not importable/
usable on the Mac this is developed on -- validate against a real Linux host before trusting it,
same discipline warden/warrant already established for their own OS-level primitives.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

#: Reserved for siphonophore-provisioned node users. Deliberately a different range from
#: warrant's own SESSION_UID_MIN/MAX (58000-58899) -- these are independent projects that may
#: run on the same host, and a shared range would risk one allocator's uid colliding with the
#: other's bookkeeping even though each only scans its own username prefix.
NODE_UID_MIN = 59000
NODE_UID_MAX = 59899

_USER_PREFIX = "siphon-node-"

#: Where provisioned nodes' cgroups live, one subdirectory per node. Configurable because the
#: cgroup v2 mount point is a deployment fact, not something this module should hardcode --
#: same env-var-with-a-sensible-default shape as warrant's WARRANT_DB_PATH/WARRANT_GATEWAY_HOST.
CGROUP_ROOT = os.environ.get("SIPHONOPHORE_CGROUP_ROOT", "/sys/fs/cgroup/siphonophore")


class IdentityError(RuntimeError):
    """A privileged operation (useradd/userdel/mkdir under cgroupfs) failed. Never swallowed --
    a failed allocation must not silently hand back an identity nothing actually provisioned."""


@dataclass(frozen=True)
class Identity:
    """A provisioned node's real OS identity. `cgroup_path` is the actual directory; `cgroup_name`
    is just its basename, kept separately since release_identity/cgroup_pids take the name, not
    the full path, mirroring how uid and username are both carried rather than re-derived."""

    node_id: str
    uid: int
    username: str
    cgroup_name: str
    cgroup_path: str


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=15)


def username_for(uid: int) -> str:
    if not (NODE_UID_MIN <= uid <= NODE_UID_MAX):
        raise ValueError(f"uid {uid} is outside the reserved node range {NODE_UID_MIN}-{NODE_UID_MAX}")
    return f"{_USER_PREFIX}{uid}"


def _existing_node_uids() -> set[int]:
    """Uids in the reserved range that already have a provisioned user -- read from /etc/passwd,
    not from this module's own bookkeeping, so a uid leaked by a crash (bookkeeping lost, OS user
    still there) is still seen as taken, never double-allocated. Same reasoning as warrant's
    uid_pool.py's _existing_session_uids."""
    taken: set[int] = set()
    with open("/etc/passwd") as f:
        for line in f:
            fields = line.rstrip("\n").split(":")
            if len(fields) < 3:
                continue
            try:
                uid = int(fields[2])
            except ValueError:
                continue
            if NODE_UID_MIN <= uid <= NODE_UID_MAX:
                taken.add(uid)
    return taken


def allocate_uid() -> int:
    """Provisions a new, no-login, no-home ephemeral OS user in the reserved range and returns its
    uid. Raises IdentityError if the range is exhausted or useradd fails for any other reason --
    an exhausted pool must surface as a real error, not silently reuse a live uid."""
    taken = _existing_node_uids()
    for uid in range(NODE_UID_MIN, NODE_UID_MAX + 1):
        if uid in taken:
            continue
        name = username_for(uid)
        result = _run(["useradd", "-M", "-N", "-s", "/usr/sbin/nologin", "-u", str(uid), name])
        if result.returncode == 0:
            return uid
        if "already exists" in result.stderr or "in use" in result.stderr:
            # Lost a race with another allocator -- try the next uid rather than erroring on a
            # transient collision.
            continue
        raise IdentityError(f"useradd failed for uid {uid}: {result.stderr.strip()}")
    raise IdentityError(f"node uid pool exhausted: no free uid in {NODE_UID_MIN}-{NODE_UID_MAX}")


def release_uid(uid: int) -> None:
    """Best-effort userdel. Never raises on 'already gone' -- a node that crashed and left no user
    to delete is not an error here. Does raise on a real privilege/tooling failure."""
    name = username_for(uid)
    result = _run(["userdel", name])
    if result.returncode != 0 and "does not exist" not in result.stderr:
        raise IdentityError(f"userdel failed for {name}: {result.stderr.strip()}")


def _ensure_cgroup_root() -> None:
    os.makedirs(CGROUP_ROOT, exist_ok=True)


def create_cgroup(name: str) -> str:
    """Creates a fresh cgroup v2 directory for one node and returns its path. Created as whatever
    uid this process is running as (expected: the broker, more privileged than any node it
    provisions) -- deliberately NOT chowned to the node's own uid afterward, so the node cannot
    write to its own cgroup.procs and move itself (or a forked child) out. Raises IdentityError if
    the directory already exists, since that means a previous node with the same name was never
    torn down -- silently reusing it would mix two nodes' descendant pids together."""
    _ensure_cgroup_root()
    path = os.path.join(CGROUP_ROOT, name)
    try:
        os.mkdir(path)
    except FileExistsError as exc:
        raise IdentityError(f"cgroup {path!r} already exists -- a previous node was not torn down") from exc
    except OSError as exc:
        raise IdentityError(f"failed to create cgroup {path!r}: {exc}") from exc
    return path


def add_pid_to_cgroup(cgroup_path: str, pid: int) -> None:
    """Moves one pid into the given cgroup. After this, any descendant that pid later forks
    inherits membership automatically -- this only needs calling once, for the node's own
    top-level spawned process, not for every descendant it creates afterward."""
    try:
        with open(os.path.join(cgroup_path, "cgroup.procs"), "w") as f:
            f.write(str(pid))
    except OSError as exc:
        raise IdentityError(f"failed to add pid {pid} to cgroup {cgroup_path!r}: {exc}") from exc


def cgroup_pids(cgroup_path: str) -> set[int]:
    """Every pid currently a member of this cgroup -- the node's originally-spawned process plus
    every live descendant that forked (with or without a later exec), because cgroup membership is
    kernel-inherited across fork. Empty once every member process has exited; does not itself mean
    the cgroup is safe to destroy for processes that exited uncleanly without the kernel yet
    reaping cgroup bookkeeping in rare races -- destroy_cgroup handles that as best-effort, not
    this function's job."""
    try:
        with open(os.path.join(cgroup_path, "cgroup.procs")) as f:
            return {int(line) for line in f if line.strip()}
    except OSError as exc:
        raise IdentityError(f"failed to read cgroup.procs at {cgroup_path!r}: {exc}") from exc


def destroy_cgroup(cgroup_path: str) -> None:
    """Best-effort rmdir. A cgroup directory can only be removed once it has zero member
    processes -- never force-kills members to get there; that's a caller decision (this module is
    provisioning primitives, not a process-lifecycle policy), not something release silently does
    on someone's behalf. Never raises on 'already gone'."""
    try:
        os.rmdir(cgroup_path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IdentityError(
            f"failed to remove cgroup {cgroup_path!r} (still has member processes?): {exc}"
        ) from exc


def provision_identity(node_id: str) -> Identity:
    """Allocates a real uid and a real cgroup for one node, before it runs any code -- identity
    assigned at provisioning time, not earned organically by a process that already exists. If
    cgroup creation fails after the uid was allocated, the uid is released before re-raising, so a
    partial-failure provision never leaks a uid nothing will ever use."""
    uid = allocate_uid()
    try:
        cgroup_path = create_cgroup(node_id)
    except IdentityError:
        release_uid(uid)
        raise
    return Identity(
        node_id=node_id,
        uid=uid,
        username=username_for(uid),
        cgroup_name=node_id,
        cgroup_path=cgroup_path,
    )


def release_identity(identity: Identity) -> None:
    """Releases both halves of a provisioned identity. Cgroup first, then uid -- destroying the
    cgroup while its user still exists is harmless; releasing the uid while the cgroup still has
    live members would orphan those members' identity without anything left to attribute them to."""
    destroy_cgroup(identity.cgroup_path)
    release_uid(identity.uid)
