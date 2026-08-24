"""The check-in half of provisioning: proof, not a self-reported label, that a spawned process
really is the node it claims to be.

Why this can't just be "the process calls an API and says who it is": neither the broker (which
only knows what it *provisioned*) nor the spawned process (which only knows what it was *told*)
can alone produce a trustworthy pairing of "this OS process really is this node" -- a bare claim
over the wire is exactly as trustworthy as a self-reported log line, which is the whole class of
problem this project exists to not repeat. It takes two independently-held pieces agreeing, the
same shape as a Kerberos ticket: the broker holds a nonce it generated and never handed to anyone
but the one process it's meant for (passed via an inherited file descriptor at spawn time, not an
env var readable by anything at the parent's privilege level via /proc/<pid>/environ); the kernel,
not the connecting process, holds the other half -- SO_PEERCRED on the Unix socket the check-in
arrives on returns the real, unspoofable uid/pid of whatever actually opened that connection,
verified by the kernel itself, not asserted by the caller. A check-in is only accepted when the
presented nonce matches what was issued for a given node AND the kernel-verified peer uid matches
the uid `identity.py` allocated for that same node. A process that skips this, or gets either half
wrong, is exactly as unverified as one that was never provisioned at all.

Linux-only (SO_PEERCRED is a Linux socket option; no equivalent on macOS). Not importable/usable
on the Mac this is developed on for its actual verification path -- but nonce generation and the
pending-registration bookkeeping are plain Python and are tested everywhere.
"""
from __future__ import annotations

import os
import secrets
import socket
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

NONCE_BYTES = 32


class CheckinError(RuntimeError):
    """A check-in was attempted but failed verification -- wrong nonce, wrong peer uid, or no
    pending registration at all. Distinct from a connection simply never arriving (which is not
    an error here -- see CheckinServer's docstring on why an unverified node just stays
    unverified, not something that raises on its own)."""


def generate_nonce() -> str:
    """A fresh, unguessable nonce for one node's check-in. Hex-encoded so it's trivially safe to
    pass as a single line of text over the check-in socket."""
    return secrets.token_hex(NONCE_BYTES)


@dataclass
class _Pending:
    node_id: str
    nonce: str
    expected_uid: int


@dataclass
class Verified:
    """Recorded once, the moment a check-in's nonce and kernel-verified peer uid both match what
    was expected. `peer_pid` is provenance (the kernel told us this, not the caller) -- not the
    primary attribution mechanism, which is the node's cgroup (identity.py); a node's own pid can
    still change across further forks the way its cgroup membership does not."""

    node_id: str
    uid: int
    peer_pid: int


def _read_peer_credentials(conn: socket.socket) -> tuple[int, int]:
    """Returns (pid, uid) from SO_PEERCRED -- the kernel's own record of who actually holds the
    other end of this socket, not anything the connecting process sent or could spoof. Linux-only;
    raises on any other platform rather than silently returning a fabricated value, since a
    fabricated peer credential would defeat the entire point of this function."""
    if sys.platform != "linux":
        raise CheckinError("SO_PEERCRED is Linux-only; cannot verify a peer's real uid on this platform")
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", creds)
    return pid, uid


class CheckinServer:
    """Listens on a Unix domain socket for check-ins. `register_pending` must be called by the
    broker before the corresponding node is spawned -- a check-in for a node nobody registered is
    rejected outright, never treated as "verified with no record of why". `on_verified` fires once
    per successful check-in, from the server's own accept-loop thread.

    A node that never checks in is not an error the server raises -- it just never appears in
    `verified`. Whether an unverified-but-provisioned node is itself a problem worth acting on is
    the broker's decision (a timeout, a policy), not something this class enforces; this class's
    only job is producing a trustworthy yes/no for "did this specific check-in verify."
    """

    def __init__(self, socket_path: str, *, on_verified: Optional[Callable[[Verified], None]] = None) -> None:
        self._socket_path = socket_path
        self._on_verified = on_verified
        self._pending: dict[str, _Pending] = {}  # keyed by nonce
        self._verified: dict[str, Verified] = {}  # keyed by node_id
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def register_pending(self, node_id: str, nonce: str, expected_uid: int) -> None:
        with self._lock:
            self._pending[nonce] = _Pending(node_id=node_id, nonce=nonce, expected_uid=expected_uid)

    def is_verified(self, node_id: str) -> bool:
        with self._lock:
            return node_id in self._verified

    def verified(self, node_id: str) -> Optional[Verified]:
        with self._lock:
            return self._verified.get(node_id)

    def start(self) -> None:
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self._socket_path)
        self._sock.listen(16)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed under us during stop()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                nonce = conn.recv(NONCE_BYTES * 2 + 16).decode().strip()
                peer_pid, peer_uid = _read_peer_credentials(conn)
            except (OSError, CheckinError, UnicodeDecodeError):
                return

            with self._lock:
                pending = self._pending.pop(nonce, None)
            if pending is None:
                return  # no registration for this nonce -- silently drop, not a verified check-in
            if pending.expected_uid != peer_uid:
                return  # nonce matched, but the kernel says a different uid actually holds it

            result = Verified(node_id=pending.node_id, uid=peer_uid, peer_pid=peer_pid)
            with self._lock:
                self._verified[pending.node_id] = result
            if self._on_verified is not None:
                self._on_verified(result)


def check_in(socket_path: str, nonce: str) -> None:
    """The client side -- what a freshly-spawned node process calls, exactly once, as its first
    action before doing any real work. A node that does this after doing other work first has
    already defeated the point: the check-in is supposed to establish trust before anything the
    node does is attributable with confidence, not after."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(socket_path)
        sock.sendall(nonce.encode())
