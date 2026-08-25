"""Tests for the check-in protocol. Nonce generation and pending-registration bookkeeping are
plain Python and run everywhere; the actual verification path needs SO_PEERCRED, which is
Linux-only -- skipped, not faked, on the Mac this was developed on. None of this needs root:
SO_PEERCRED reports whatever uid this test process is already running as, which is enough to
prove the wiring is correct even without a real uid switch (that's identity.py's job, tested
there with real root)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import pytest

from siphonophore.checkin import (
    CheckinError,
    CheckinServer,
    NONCE_BYTES,
    Verified,
    _read_peer_credentials,
    check_in,
    generate_nonce,
)

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="SO_PEERCRED is Linux-only")


def test_generate_nonce_is_the_right_length_and_actually_random():
    a, b = generate_nonce(), generate_nonce()
    assert len(a) == NONCE_BYTES * 2  # hex-encoded
    assert a != b


def test_read_peer_credentials_raises_rather_than_fabricating_a_value_on_non_linux():
    """The actual regression this pins: on a non-Linux host (this Mac, right now -- not
    simulated), there's no way to get a real, kernel-verified peer credential, so this must raise
    rather than silently returning something that looks plausible but isn't independently
    verified. Runs everywhere; only exercises the raise branch on non-Linux."""
    if sys.platform == "linux":
        pytest.skip("this pins the non-Linux behavior specifically")
    import socket as socket_module

    a, b = socket_module.socketpair()
    try:
        with pytest.raises(CheckinError):
            _read_peer_credentials(a)
    finally:
        a.close()
        b.close()


def _socket_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "siphonophore-test.sock")


@linux_only
def test_checkin_verifies_when_nonce_and_peer_uid_both_match():
    path = _socket_path()
    server = CheckinServer(path)
    server.start()
    try:
        nonce = generate_nonce()
        server.register_pending("node-a", nonce, expected_uid=os.getuid())
        check_in(path, nonce)

        deadline = time.time() + 2
        while time.time() < deadline and not server.is_verified("node-a"):
            time.sleep(0.01)

        assert server.is_verified("node-a")
        result = server.verified("node-a")
        assert isinstance(result, Verified)
        assert result.uid == os.getuid()
        assert result.peer_pid == os.getpid()
    finally:
        server.stop()


@linux_only
def test_checkin_is_rejected_when_no_registration_exists_for_the_nonce():
    path = _socket_path()
    server = CheckinServer(path)
    server.start()
    try:
        check_in(path, generate_nonce())  # never registered
        time.sleep(0.1)
        assert not server.is_verified("node-a")
    finally:
        server.stop()


@linux_only
def test_checkin_is_rejected_when_the_kernel_verified_uid_does_not_match():
    """The actual proof this isn't a self-reported check: register an expected uid that is
    provably not this test process's real uid, present the right nonce anyway, and confirm the
    mismatch is caught by the kernel-verified peer credential, not the caller's claim (there is no
    uid field in the check-in message at all -- the caller cannot claim a uid even if it wanted
    to)."""
    path = _socket_path()
    server = CheckinServer(path)
    server.start()
    try:
        nonce = generate_nonce()
        wrong_uid = os.getuid() + 999999
        server.register_pending("node-a", nonce, expected_uid=wrong_uid)
        check_in(path, nonce)
        time.sleep(0.1)
        assert not server.is_verified("node-a")
    finally:
        server.stop()
