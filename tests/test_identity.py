"""Tests for the portable parts of identity.py: nonce generation/delivery and CheckinRegistry's
routing/verification logic. All of this is plain Python (threading.Event, dicts) with no sockets
and no Linux dependency -- CheckinListener and read_peer_uid need SO_PEERCRED and are tested
separately in test_identity_linux.py (linux_root_only)."""
from __future__ import annotations

import os
import threading

import pytest

from siphonophore_core.identity import CheckinRegistry, IdentityError, generate_nonce, nonce_pipe, read_nonce_from_fd


def test_generate_nonce_is_long_and_unique():
    a, b = generate_nonce(), generate_nonce()
    assert len(a) >= 32
    assert a != b


def test_nonce_pipe_round_trips_through_read_nonce_from_fd():
    nonce = generate_nonce()
    read_fd, write_fd = nonce_pipe(nonce)
    os.close(write_fd)
    assert read_nonce_from_fd(read_fd) == nonce


def test_register_then_matching_checkin_verifies():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    verified, matched_id, reason = registry.handle_checkin("nonce-a", peer_uid=1000)
    assert verified is True
    assert matched_id == "exec-1"
    assert reason == "ok"


def test_unknown_nonce_refused_without_matching_any_registration():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    verified, matched_id, reason = registry.handle_checkin("nonce-that-was-never-registered", peer_uid=1000)
    assert verified is False
    assert matched_id is None
    assert reason == "no pending registration for this nonce"


def test_correct_nonce_wrong_uid_refused_but_matched_and_recorded():
    """A leaked-nonce-but-wrong-uid attempt is routed to the right registration (so it shows up in
    the eventual result) but must not consume or fail that registration -- the real owner can still
    check in afterward."""
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    verified, matched_id, reason = registry.handle_checkin("nonce-a", peer_uid=9999)
    assert verified is False
    assert matched_id == "exec-1"
    assert reason == "uid mismatch"

    # the real owner still succeeds afterward
    verified2, matched_id2, _ = registry.handle_checkin("nonce-a", peer_uid=1000)
    assert verified2 is True
    assert matched_id2 == "exec-1"


def test_wait_for_result_records_rejected_attempts_and_final_success():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    registry.handle_checkin("nonce-a", peer_uid=9999)  # rogue attempt first
    registry.handle_checkin("nonce-a", peer_uid=1000)  # then the real owner

    result = registry.wait_for_result("nonce-a", timeout=1.0)
    assert result["verified"] is True
    assert result["execution_id"] == "exec-1"
    assert result["rejected_attempts"] == [{"peer_uid": 9999}]


def test_wait_for_result_times_out_when_nobody_checks_in():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    result = registry.wait_for_result("nonce-a", timeout=0.2)
    assert result["verified"] is False
    assert result["reason"] == "timeout"


def test_registration_is_one_shot_consumed_by_wait_for_result():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    registry.handle_checkin("nonce-a", peer_uid=1000)
    registry.wait_for_result("nonce-a", timeout=1.0)
    assert registry.pending_count() == 0

    # nonce-a no longer routes to anything -- a second presentation is refused, not re-verified
    verified, matched_id, reason = registry.handle_checkin("nonce-a", peer_uid=1000)
    assert verified is False
    assert matched_id is None


def test_already_finalized_registration_refuses_a_second_checkin():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    registry.handle_checkin("nonce-a", peer_uid=1000)  # finalizes it
    verified, matched_id, reason = registry.handle_checkin("nonce-a", peer_uid=1000)
    assert verified is False
    assert matched_id == "exec-1"
    assert reason == "registration already finalized"


def test_nonce_collision_on_register_raises():
    registry = CheckinRegistry()
    registry.register_pending("exec-1", "nonce-a", expected_uid=1000)
    with pytest.raises(IdentityError):
        registry.register_pending("exec-2", "nonce-a", expected_uid=2000)


def test_concurrent_registrations_route_independently_no_cross_attribution():
    """Many pending registrations at once, checked in from many threads in a shuffled order --
    each must resolve to its own execution_id, never another's (the property lab/006 proved under
    real subprocess concurrency; this is the same routing logic under thread concurrency)."""
    registry = CheckinRegistry()
    n = 50
    for i in range(n):
        registry.register_pending(f"exec-{i}", f"nonce-{i}", expected_uid=1000 + i)

    results: list[tuple[bool, str | None, str]] = [None] * n  # type: ignore[list-item]

    def _checkin(i: int) -> None:
        results[i] = registry.handle_checkin(f"nonce-{i}", peer_uid=1000 + i)

    threads = [threading.Thread(target=_checkin, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(verified for verified, _, _ in results)
    assert all(matched_id == f"exec-{i}" for i, (_, matched_id, _) in enumerate(results))
