"""Real-substrate tests for uid + cgroup provisioning. Needs real root on real Linux (useradd,
userdel, and write access to /sys/fs/cgroup) -- skipped, not faked, everywhere else."""
from __future__ import annotations

import os
import sys

import pytest

from siphonophore.identity import (
    IdentityError,
    add_pid_to_cgroup,
    allocate_uid,
    cgroup_pids,
    create_cgroup,
    destroy_cgroup,
    provision_identity,
    release_identity,
    release_uid,
    username_for,
)

linux_root_only = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="provisions real OS users and real cgroups, needs real root on real Linux",
)


def test_username_for_rejects_uid_outside_the_reserved_range():
    with pytest.raises(ValueError):
        username_for(1)


@linux_root_only
def test_allocate_and_release_uid_round_trip():
    uid = allocate_uid()
    try:
        assert 59000 <= uid <= 59899
        with open("/etc/passwd") as f:
            assert any(f":{uid}:" in line for line in f)
    finally:
        release_uid(uid)
    with open("/etc/passwd") as f:
        assert not any(username_for(uid) in line for line in f)


@linux_root_only
def test_release_uid_is_idempotent_on_an_already_gone_user():
    uid = allocate_uid()
    release_uid(uid)
    release_uid(uid)  # must not raise the second time


@linux_root_only
def test_create_cgroup_refuses_to_reuse_an_existing_directory():
    path = create_cgroup("test-node-dup")
    try:
        with pytest.raises(IdentityError):
            create_cgroup("test-node-dup")
    finally:
        destroy_cgroup(path)


@linux_root_only
def test_cgroup_tracks_a_grandchild_that_was_never_added_individually():
    """The actual proof cgroup membership survives fork without exec -- the specific gap a bare
    pid can't close. Everything happens inside a forked child (the pytest process itself never
    joins the cgroup, so this can't disturb the rest of the test run): the child joins the cgroup,
    then forks a grandchild that is never itself added to anything. A pipe -- not a sleep -- proves
    the grandchild is alive and past its fork point before the parent reads cgroup membership, so
    this isn't a timing race."""
    read_fd, write_fd = os.pipe()
    path = create_cgroup("test-node-fork")
    try:
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            add_pid_to_cgroup(path, os.getpid())
            grandchild_pid = os.fork()
            if grandchild_pid == 0:
                os.write(write_fd, b"x")  # proves this grandchild is alive, past its own fork
                os.close(write_fd)
                import time

                time.sleep(1)  # stay alive long enough for the parent to observe it
                os._exit(0)
            os.close(write_fd)
            os.waitpid(grandchild_pid, 0)
            os._exit(0)

        os.close(write_fd)
        assert os.read(read_fd, 1) == b"x"  # blocks until the grandchild has actually forked
        members = cgroup_pids(path)
        os.waitpid(child_pid, 0)
        # The exact grandchild pid isn't observable from here without extra plumbing back through
        # the pipe -- the meaningful assertion is that more than just the one child pid we know
        # about ended up a member, since only the child was ever explicitly added.
        assert len(members - {child_pid}) >= 1, "a forked grandchild must appear without being added individually"
    finally:
        os.close(read_fd)
        destroy_cgroup(path)


@linux_root_only
def test_provision_and_release_identity_round_trip():
    identity = provision_identity("test-node-full")
    try:
        assert 59000 <= identity.uid <= 59899
        assert os.path.isdir(identity.cgroup_path)
    finally:
        release_identity(identity)
    assert not os.path.isdir(identity.cgroup_path)
