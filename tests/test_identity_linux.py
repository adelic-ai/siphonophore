"""Tests for CheckinListener + real SO_PEERCRED-verified check-ins. linux_root_only: needs real
root on real Linux (useradd/userdel, cgroup v2) to provision genuinely distinct uids -- run these
on colima. This is a condensed version of lab/006's predicate A (concurrent registrations routed
correctly, zero cross-attribution) and predicate B (a real cross-identity uid mismatch is rejected
and does not block the real owner's own check-in), built against the package's permanent
identity.py rather than lab/006's standalone script."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from siphonophore_core.execution_uid_cgroup import (
    ProvisioningError,
    add_pid_to_cgroup,
    provision_cgroup,
    provision_ephemeral_user,
    release_cgroup,
    release_ephemeral_user,
    require_real_root_linux,
)
from siphonophore_core.identity import CheckinListener, CheckinRegistry, generate_nonce, nonce_pipe

pytestmark = pytest.mark.linux_root_only


def _preconditions_met() -> bool:
    try:
        require_real_root_linux()
    except ProvisioningError:
        return False
    return True


requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason="needs real root on real Linux with cgroup v2 (run on colima)")

# Distinct range from every lab experiment and from every other siphonophore_core test file's
# range, so a run here can never collide with leftover state from any of them.
UID_MIN = 62500
UID_MAX = 62599
CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-core-identity-tests")

_CHILD_PROGRAM = """
import json, os, sys
from siphonophore_core.identity import perform_checkin, read_nonce_from_fd

socket_path, out_path, nonce_fd = sys.argv[1], sys.argv[2], int(sys.argv[3])
nonce = read_nonce_from_fd(nonce_fd)
verified = perform_checkin(socket_path, nonce)
if verified:
    with open(out_path, "w") as f:
        f.write("content-from-uid-" + str(os.getuid()))
print(json.dumps({"checked_in": verified, "pid": os.getpid(), "self_reported_uid": os.getuid()}))
"""

_ROGUE_PROGRAM = """
import sys
from siphonophore_core.identity import perform_checkin

socket_path, nonce = sys.argv[1], sys.argv[2]
sys.stdout.write("1" if perform_checkin(socket_path, nonce) else "0")
"""


@pytest.fixture
def world_writable_dir():
    """pytest's own tmp_path lives under /tmp/pytest-of-root/ (mode 700, root-owned) -- a
    provisioned unprivileged uid can't traverse into it at all. Same fix every uid_cgroup lab
    experiment needed for its own workdir."""
    d = Path(tempfile.mkdtemp(prefix="sipho-core-identity-test-"))
    os.chmod(d, 0o777)
    yield d
    shutil.rmtree(d, ignore_errors=True)


class _Identity:
    def __init__(self, label: str) -> None:
        # execution_id is provision_ephemeral_user's username source (truncated to 8 chars) --
        # uuid4().hex alone keeps that truncation collision-free regardless of how many identities
        # a single test provisions, unlike a shared human-readable prefix (e.g. "concurrent-0",
        # "concurrent-1", ... all truncate to the same 8 chars and collide on useradd).
        self.execution_id = uuid.uuid4().hex
        self.label = label
        self.username, self.uid, self.gid = provision_ephemeral_user(self.execution_id, UID_MIN, UID_MAX)
        self.cgroup_path = provision_cgroup(CGROUP_ROOT, self.execution_id)

    def release(self) -> None:
        release_cgroup(self.cgroup_path)
        release_ephemeral_user(self.username)


def _spawn_child(identity: _Identity, socket_path: str, out_path: Path, nonce: str) -> subprocess.Popen:
    read_fd, write_fd = nonce_pipe(nonce)

    def _drop_privileges() -> None:
        os.setgroups([])
        os.setgid(identity.gid)
        os.setuid(identity.uid)

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_PROGRAM, socket_path, str(out_path), str(read_fd)],
        pass_fds=(read_fd,), preexec_fn=_drop_privileges,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    os.close(read_fd)
    os.close(write_fd)
    add_pid_to_cgroup(identity.cgroup_path, proc.pid)
    return proc


@requires_root_linux
def test_single_real_checkin_verified_by_kernel_uid(world_writable_dir: Path):
    registry = CheckinRegistry()
    socket_path = f"/tmp/sipho-core-identity-test-{uuid.uuid4().hex[:8]}.sock"
    identity = _Identity("single")
    out_path = world_writable_dir / "effect.txt"
    nonce = generate_nonce()
    registry.register_pending(identity.execution_id, nonce, expected_uid=identity.uid)

    with CheckinListener(socket_path, registry):
        proc = _spawn_child(identity, socket_path, out_path, nonce)
        result = registry.wait_for_result(nonce, timeout=5.0)
        stdout, _stderr = proc.communicate(timeout=5)

    self_report = json.loads(stdout.strip())
    identity.release()

    assert result["verified"] is True
    assert result["execution_id"] == identity.execution_id
    assert self_report["checked_in"] is True
    assert self_report["self_reported_uid"] == identity.uid  # child's own view
    assert out_path.read_text() == f"content-from-uid-{identity.uid}"  # real effect on disk


@requires_root_linux
def test_concurrent_real_identities_route_with_zero_cross_attribution(world_writable_dir: Path):
    n = 3
    registry = CheckinRegistry()
    socket_path = f"/tmp/sipho-core-identity-test-{uuid.uuid4().hex[:8]}.sock"
    identities = [_Identity(f"concurrent-{i}") for i in range(n)]
    nonces = [generate_nonce() for _ in range(n)]
    out_paths = [world_writable_dir / f"effect-{i}.txt" for i in range(n)]
    for identity, nonce in zip(identities, nonces):
        registry.register_pending(identity.execution_id, nonce, expected_uid=identity.uid)

    try:
        with CheckinListener(socket_path, registry):
            procs = [
                _spawn_child(identity, socket_path, out_path, nonce)
                for identity, out_path, nonce in zip(identities, out_paths, nonces)
            ]

            results: list[dict | None] = [None] * n

            def _wait(i: int) -> None:
                results[i] = registry.wait_for_result(nonces[i], timeout=5.0)

            threads = [threading.Thread(target=_wait, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            for proc in procs:
                proc.communicate(timeout=5)
    finally:
        for identity in identities:
            identity.release()

    assert all(r["verified"] is True for r in results)
    assert all(r["execution_id"] == identities[i].execution_id for i, r in enumerate(results))
    for i, identity in enumerate(identities):
        assert out_paths[i].read_text() == f"content-from-uid-{identity.uid}"


@requires_root_linux
def test_cross_identity_wrong_uid_rejected_real_owner_still_succeeds(world_writable_dir: Path):
    registry = CheckinRegistry()
    socket_path = f"/tmp/sipho-core-identity-test-{uuid.uuid4().hex[:8]}.sock"
    identity_a = _Identity("crossA")
    identity_b = _Identity("crossB")
    nonce_a = generate_nonce()
    nonce_b = generate_nonce()
    registry.register_pending(identity_a.execution_id, nonce_a, expected_uid=identity_a.uid)
    registry.register_pending(identity_b.execution_id, nonce_b, expected_uid=identity_b.uid)

    try:
        with CheckinListener(socket_path, registry) as listener:
            # B's real uid presents A's real nonce -- a partial-compromise attempt: nonce known,
            # uid not controlled.
            out_fd, out_file = tempfile.mkstemp(prefix="sipho-core-identity-rogue-")

            def _drop_b() -> None:
                os.setgroups([])
                os.setgid(identity_b.gid)
                os.setuid(identity_b.uid)

            try:
                subprocess.run(
                    [sys.executable, "-c", _ROGUE_PROGRAM, socket_path, nonce_a],
                    preexec_fn=_drop_b, stdout=out_fd, stderr=subprocess.DEVNULL, timeout=5,
                )
                rogue_response = Path(out_file).read_text()
            finally:
                os.close(out_fd)
                os.unlink(out_file)

            rogue_entries = [
                c for c in listener.connections_handled
                if c["peer_uid"] == identity_b.uid and c["reason"] == "uid mismatch"
            ]

            out_a = world_writable_dir / "effect-a.txt"
            out_b = world_writable_dir / "effect-b.txt"
            proc_a = _spawn_child(identity_a, socket_path, out_a, nonce_a)
            proc_b = _spawn_child(identity_b, socket_path, out_b, nonce_b)
            result_a = registry.wait_for_result(nonce_a, timeout=5.0)
            result_b = registry.wait_for_result(nonce_b, timeout=5.0)
            proc_a.communicate(timeout=5)
            proc_b.communicate(timeout=5)
    finally:
        identity_a.release()
        identity_b.release()

    assert rogue_response == "0"
    assert len(rogue_entries) == 1
    assert rogue_entries[0]["matched_execution_id"] == identity_a.execution_id

    assert result_a["verified"] is True  # A's genuine check-in still succeeds after the rogue attempt
    assert len(result_a["rejected_attempts"]) == 1
    assert result_a["rejected_attempts"][0]["peer_uid"] == identity_b.uid

    assert result_b["verified"] is True  # B's own genuine check-in succeeds independently
    assert result_b["rejected_attempts"] == []

    assert out_a.read_text() == f"content-from-uid-{identity_a.uid}"
    assert out_b.read_text() == f"content-from-uid-{identity_b.uid}"
