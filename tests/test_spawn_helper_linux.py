"""Tests for siphonophore-spawn (contracts/spawn_helper.md, SH-01..SH-26). linux_root_only: needs
real root on real Linux (memfd_create, cgroup v2, a real passwd entry) -- run on colima, matching
every other real-root suite's own discipline. Skipped automatically anywhere the preconditions
aren't real.

Runs the compiled binary directly as root, not through `sudo -n` -- that inner layer is what these
tests exercise (envelope parsing, identity cross-validation, one-shot cgroup binding, sealed-memfd
handoff, privilege drop, argv-avoidance). The outer `sudo`-argument-free enforcement (SH-08) is a
deployment/sudoers-config property, not something this binary's own logic can prove about itself --
that's validated manually against a real sudoers grant and recorded in HISTORY.md, the same way the
useradd/userdel privilege-separation grant was, rather than provisioned automatically in CI.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.linux_root_only

SPAWN_HELPER_DIR = Path(__file__).resolve().parent.parent / "spawn_helper"
HELPER_BINARY = "/usr/local/libexec/siphonophore-spawn"
BOOTSTRAP_SCRIPT = "/usr/local/libexec/siphonophore-bootstrap.py"

# Distinct range from execution_uid_cgroup's own default (60000-60999) and from
# test_execution_uid_cgroup.py's test range (61500-61599) -- no collision with anything else that
# might be running against the same colima host.
TEST_UID = 62500
TEST_USERNAME = "sipho-core-spwntst"
CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-core")  # hardcoded in the C source, not configurable


def _preconditions_met() -> bool:
    return sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0


skip_reason = "needs real root on real Linux (run on colima)"
requires_root_linux = pytest.mark.skipif(not _preconditions_met(), reason=skip_reason)


@pytest.fixture(scope="module")
def built_and_installed():
    """Builds and installs the helper + bootstrap runtime once per test module run. Requires real
    root (already a module-level precondition) since install must produce root-owned files
    (SH-26)."""
    subprocess.run(["make", "-C", str(SPAWN_HELPER_DIR)], check=True, capture_output=True)
    subprocess.run(["make", "-C", str(SPAWN_HELPER_DIR), "install"], check=True, capture_output=True)
    assert Path(HELPER_BINARY).exists()
    assert Path(BOOTSTRAP_SCRIPT).exists()


@pytest.fixture
def ephemeral_user(built_and_installed):
    """A real system user matching the identity siphonophore-spawn is asked to spawn as --
    provisioned directly via the useradd wrapper script (no elevation prefix needed, tests
    already run as real root), released unconditionally afterward."""
    useradd = str(SPAWN_HELPER_DIR.parent / "scripts" / "siphonophore-useradd")
    userdel = str(SPAWN_HELPER_DIR.parent / "scripts" / "siphonophore-userdel")
    subprocess.run([useradd, str(TEST_UID), TEST_USERNAME], check=True, capture_output=True)
    try:
        yield TEST_USERNAME, TEST_UID
    finally:
        subprocess.run([userdel, TEST_USERNAME], capture_output=True)


def _frame(env: dict, source: bytes, payload: bytes, nonce: bytes = b"", trailing: bytes = b"") -> bytes:
    return json.dumps(env).encode() + b"\n" + source + payload + nonce + trailing


def _envelope(execution_id: str, username: str, uid: int, source: bytes, payload: bytes,
              nonce: bytes = b"", **overrides) -> dict:
    env = {
        "version": 1, "uid": uid, "username": username, "execution_id": execution_id,
        "code_length": len(source), "payload_length": len(payload), "nonce_length": len(nonce),
    }
    env.update(overrides)
    return env


def _run_helper(stream: bytes, timeout: float = 25.0) -> subprocess.CompletedProcess:
    return subprocess.run([HELPER_BINARY], input=stream, capture_output=True, timeout=timeout)


def _cleanup_leaf(execution_id: str) -> None:
    leaf = CGROUP_ROOT / f"exec-{execution_id}"
    try:
        leaf.rmdir()
    except OSError:
        pass  # not present, or a test already exercised its own cleanup path -- either is fine


# ---- happy path: SH-01..SH-11, SH-20, SH-23, SH-24, SH-25 all have to cooperate for this to work

@requires_root_linux
def test_full_spawn_drops_privilege_joins_cgroup_and_passes_payload(ephemeral_user):
    username, uid = ephemeral_user
    execution_id = "pytest-happy-001"
    source = (
        b"import os, json\n"
        b"print(json.dumps({'uid': os.getuid(), 'euid': os.geteuid(), "
        b"'cgroup': open('/proc/self/cgroup').read().strip(), 'payload': payload}))\n"
    )
    payload = json.dumps({"k": "v"}).encode()
    env = _envelope(execution_id, username, uid, source, payload)
    try:
        result = _run_helper(_frame(env, source, payload))
        assert result.returncode == 0, result.stderr.decode()
        out = json.loads(result.stdout)
        assert out["uid"] == uid
        assert out["euid"] == uid
        assert f"exec-{execution_id}" in out["cgroup"]
        assert out["payload"] == {"k": "v"}
    finally:
        _cleanup_leaf(execution_id)


@requires_root_linux
def test_source_and_payload_fds_are_read_only_to_the_artifact(ephemeral_user):
    """SH-25: sealed AND exposed read-only -- the artifact must not be able to write to either
    fd, not just find them sealed against shrink/grow."""
    username, uid = ephemeral_user
    execution_id = "pytest-readonly-001"
    source = (
        b"import os\n"
        b"results = {}\n"
        b"for fd, label in ((3, 'source'), (4, 'payload')):\n"
        b"    try:\n"
        b"        os.write(fd, b'x')\n"
        b"        results[label] = 'WRITE SUCCEEDED'\n"
        b"    except OSError as e:\n"
        b"        results[label] = str(e.errno)\n"
        b"import json\n"
        b"print(json.dumps(results))\n"
    )
    payload = b"{}"
    env = _envelope(execution_id, username, uid, source, payload)
    try:
        result = _run_helper(_frame(env, source, payload))
        assert result.returncode == 0, result.stderr.decode()
        out = json.loads(result.stdout)
        assert out["source"] != "WRITE SUCCEEDED"
        assert out["payload"] != "WRITE SUCCEEDED"
    finally:
        _cleanup_leaf(execution_id)


@requires_root_linux
def test_argv_never_carries_source_or_payload_content(ephemeral_user):
    """SH-22, verified against /proc/<pid>/cmdline directly rather than assumed."""
    username, uid = ephemeral_user
    execution_id = "pytest-argv-001"
    marker = b"UNIQUE_MARKER_SHOULD_NEVER_APPEAR_IN_ARGV"
    source = (
        b"cmdline = open('/proc/self/cmdline', 'rb').read()\n"
        b"print('MARKER_FOUND' if b'" + marker + b"' in cmdline else 'MARKER_ABSENT')\n"
    )
    payload = json.dumps({"marker": marker.decode()}).encode()
    env = _envelope(execution_id, username, uid, source, payload)
    try:
        result = _run_helper(_frame(env, source, payload))
        assert result.returncode == 0, result.stderr.decode()
        assert result.stdout.strip() == b"MARKER_ABSENT"
    finally:
        _cleanup_leaf(execution_id)


# ---- SH-23: one-shot execution binding ------------------------------------------------------

@requires_root_linux
def test_replaying_the_same_execution_id_is_refused(ephemeral_user):
    username, uid = ephemeral_user
    execution_id = "pytest-replay-001"
    source = b"pass\n"
    payload = b"{}"
    env = _envelope(execution_id, username, uid, source, payload)
    try:
        first = _run_helper(_frame(env, source, payload))
        assert first.returncode == 0, first.stderr.decode()
        second = _run_helper(_frame(env, source, payload))
        assert second.returncode == 23
        assert b"already consumed" in second.stderr
    finally:
        _cleanup_leaf(execution_id)


@requires_root_linux
def test_a_failure_after_joining_the_cgroup_still_removes_the_leaf(ephemeral_user):
    """Regression test for a real bug found during manual validation: a failure discovered AFTER
    SH-23 has already added this process to the cgroup leaf could not rmdir it (cgroup v2 refuses
    to remove a non-empty leaf, and this process is still a member of it at that exact moment) --
    fixed by moving the process back to the parent cgroup before removing the leaf."""
    username, uid = ephemeral_user
    execution_id = "pytest-cleanup-001"
    env = _envelope(execution_id, username, uid, source=b"unused", payload=b"{}",
                     code_length=999999999)  # SH-14: fails after cgroup join, before body reads
    result = _run_helper(_frame(env, b"", b""))
    assert result.returncode == 14
    leaf = CGROUP_ROOT / f"exec-{execution_id}"
    assert not leaf.exists(), "cgroup leaf survived a post-join failure -- cleanup regressed"


# ---- fail-closed conditions, one case per SH-NN --------------------------------------------

@requires_root_linux
def test_sh12_unsupported_version(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("pytest-sh12", username, uid, b"pass\n", b"{}", version=2)
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 12


@requires_root_linux
def test_sh13_malformed_envelope():
    result = _run_helper(b"not json\npass\n{}")
    assert result.returncode == 13


@requires_root_linux
def test_sh13_unknown_key(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("pytest-sh13", username, uid, b"pass\n", b"{}")
    env["unexpected_field"] = "nope"
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 13


@requires_root_linux
def test_sh14_oversized_code_length(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("pytest-sh14", username, uid, b"pass\n", b"{}", code_length=999999999)
    result = _run_helper(_frame(env, b"", b""))
    assert result.returncode == 14


@requires_root_linux
def test_sh15_short_read(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("pytest-sh15", username, uid, b"pass\n", b"{}", code_length=1000)
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 15


@requires_root_linux
def test_sh16_trailing_bytes(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("pytest-sh16", username, uid, b"pass\n", b"{}")
    result = _run_helper(_frame(env, b"pass\n", b"{}", trailing=b"extra"))
    assert result.returncode == 16


@requires_root_linux
def test_sh17_uid_out_of_range(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("pytest-sh17a", username, 100, b"pass\n", b"{}")
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 17


@requires_root_linux
def test_sh17_username_fails_naming_convention(ephemeral_user):
    _username, uid = ephemeral_user
    env = _envelope("pytest-sh17b", "root", uid, b"pass\n", b"{}")
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 17


@requires_root_linux
def test_sh17_uid_username_mismatch(ephemeral_user):
    username, _uid = ephemeral_user
    env = _envelope("pytest-sh17c", username, 60999, b"pass\n", b"{}")
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 17


@requires_root_linux
def test_sh17_execution_id_path_traversal_rejected(ephemeral_user):
    username, uid = ephemeral_user
    env = _envelope("../../etc", username, uid, b"pass\n", b"{}")
    result = _run_helper(_frame(env, b"pass\n", b"{}"))
    assert result.returncode == 17


# ---- SH-21: liveness -- a genuinely blocked client must not hang the helper forever ----------

@requires_root_linux
def test_sh21_blocked_client_times_out_and_cleans_up(ephemeral_user):
    """Real timing test, not simulated: opens a pipe, writes a complete envelope header (so the
    process gets past identity validation and joins its cgroup), then withholds the declared
    source bytes and keeps the write end open (no EOF) so the helper genuinely blocks in
    read_exact(). Confirms the SIGALRM path both fires and leaves no cgroup leaf behind -- this
    exercises the one code path (a signal handler) unit tests can't reach any other way."""
    username, uid = ephemeral_user
    execution_id = "pytest-timeout-001"
    env = _envelope(execution_id, username, uid, b"x" * 100, b"{}")
    header = json.dumps(env).encode() + b"\n"

    read_fd, write_fd = os.pipe()
    os.write(write_fd, header)  # header only -- never send the declared 100 source bytes

    start = time.monotonic()
    try:
        proc = subprocess.Popen([HELPER_BINARY], stdin=read_fd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        os.close(read_fd)
        returncode = proc.wait(timeout=30)
        elapsed = time.monotonic() - start
        assert returncode == 21
        assert elapsed >= 15, "timeout fired too early -- SIGALRM misconfigured"
    finally:
        os.close(write_fd)
        leaf = CGROUP_ROOT / f"exec-{execution_id}"
        assert not leaf.exists(), "cgroup leaf survived a SIGALRM timeout -- signal-handler cleanup regressed"
