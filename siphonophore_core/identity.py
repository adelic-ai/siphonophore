"""Check-in identity verification (DESIGN.md section 6's `identity/` module; lab/005, lab/006).

A spawned process is not trusted merely for having been spawned -- delegation specifically means
the broker hands off to a separately-dispatched sub-agent and only finds out it's legitimate by
independently verifying it. That verification is two kernel-grounded facts, both required: the
process holds a nonce the broker delivered to it via an inherited pipe fd (never argv, which is
world-readable via /proc/<pid>/cmdline on real Linux -- lab/005's finding, learned the hard way in
v1), and SO_PEERCRED -- the kernel's own record of which real uid holds the other end of a Unix
socket connection, not anything the connecting process asserts about itself. Both must agree
before the broker trusts a delegated sub-agent's check-in.

Consolidates lab/005 (one dedicated socket per delegation, trivially safe by construction -- no
shared resource to confuse) and lab/006 (one shared listener serving many concurrent delegations
at once, routed strictly by nonce, tested under genuine concurrency) into the package's permanent
identity primitive. Built fresh here, not imported from lab/ or any prior architecture.

`CheckinRegistry`'s routing/verification logic is pure Python (no sockets, no Linux dependency) and
is exercised directly by the portable test suite. `CheckinListener` and `read_peer_uid` need
SO_PEERCRED, which is Linux-specific -- they fail loudly and immediately anywhere else, never
silently degrading to a weaker check.
"""
from __future__ import annotations

import os
import secrets
import socket
import struct
import sys
import threading
from dataclasses import dataclass, field


class IdentityError(RuntimeError):
    """A check-in failed to establish who a spawned process really is. Distinct from
    GateViolation, which means a Decision itself was never trustworthy -- IdentityError means the
    Decision was fine but the process claiming to act on it couldn't prove it was the one."""


def generate_nonce() -> str:
    return secrets.token_hex(32)


def nonce_pipe(nonce: str) -> tuple[int, int]:
    """Write `nonce` into one end of a fresh pipe and return (read_fd, write_fd). Pass read_fd to
    the spawned child via subprocess.Popen(pass_fds=(read_fd,)) -- never argv. The caller closes
    both fds in the parent once the child has been spawned (its own dup'd copy of read_fd stays
    open in the child); the child reads and closes read_fd itself, see read_nonce_from_fd()."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, nonce.encode())
    return read_fd, write_fd


def read_nonce_from_fd(nonce_fd: int) -> str:
    """Child-side: read the nonce the broker delivered via nonce_pipe(), and close the fd."""
    nonce = os.read(nonce_fd, 4096).decode()
    os.close(nonce_fd)
    return nonce


def read_peer_uid(conn: socket.socket) -> int:
    """The kernel's own record of which real uid holds the other end of this Unix socket
    connection -- SO_PEERCRED, not anything the connecting process sent or could spoof.
    Linux-only: SO_PEERCRED has no portable equivalent."""
    if sys.platform != "linux":
        raise IdentityError(f"SO_PEERCRED requires real Linux; detected sys.platform={sys.platform!r}")
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def perform_checkin(socket_path: str, nonce: str) -> bool:
    """Child-side: connect to the broker's CheckinListener, present `nonce`, and return whether
    the broker verified it. Called from the spawned sub-agent's own process, a different Python
    interpreter than the broker's -- this is the child-side half of the protocol
    CheckinRegistry/CheckinListener implement on the broker side."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        sock.sendall(nonce.encode())
        response = sock.recv(1)
    finally:
        sock.close()
    return response == b"1"


@dataclass
class _PendingCheckin:
    execution_id: str
    expected_uid: int
    event: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    rejected_attempts: list = field(default_factory=list)


class CheckinRegistry:
    """Holds multiple pending check-in registrations at once, keyed by nonce (lab/006).

    A connection is matched to a registration purely by the nonce it presents -- never by
    connection order, never by uid alone. Once matched, the kernel-reported peer uid for that
    connection must equal the registration's expected_uid for the check-in to verify.

    A matched-nonce-but-wrong-uid attempt is recorded against that registration but does not
    consume or fail it outright -- the real owner may still present the correct nonce from the
    correct uid before the overall timeout. This means a leaked nonce alone cannot be used to lock
    the real delegation out of its own check-in window merely by an attacker connecting first with
    the wrong uid (a leaked nonce plus a matching uid would succeed -- that is a different,
    already-understood threat covered by nonce secrecy via nonce_pipe's inherited-fd delivery)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingCheckin] = {}

    def register_pending(self, execution_id: str, nonce: str, expected_uid: int) -> None:
        with self._lock:
            if nonce in self._pending:
                raise IdentityError(f"nonce collision registering execution_id={execution_id!r}")
            self._pending[nonce] = _PendingCheckin(execution_id=execution_id, expected_uid=expected_uid)

    def handle_checkin(self, presented_nonce: str, peer_uid: int) -> tuple[bool, str | None, str]:
        """Returns (verified, matched_execution_id, reason). Routing is entirely by
        presented_nonce -- connection order and peer_uid play no role in WHICH registration is
        consulted, only in whether that one registration's own uid check passes."""
        with self._lock:
            entry = self._pending.get(presented_nonce)
            if entry is None:
                return False, None, "no pending registration for this nonce"
            if entry.result is not None:
                return False, entry.execution_id, "registration already finalized"
            if entry.expected_uid != peer_uid:
                entry.rejected_attempts.append({"peer_uid": peer_uid})
                return False, entry.execution_id, "uid mismatch"
            entry.result = {"verified": True, "peer_uid": peer_uid}
            entry.event.set()
            return True, entry.execution_id, "ok"

    def wait_for_result(self, nonce: str, timeout: float) -> dict:
        with self._lock:
            entry = self._pending.get(nonce)
        if entry is None:
            return {"verified": False, "reason": "no such registration"}
        got = entry.event.wait(timeout)
        with self._lock:
            if not got:
                result = {"verified": False, "reason": "timeout"}
            else:
                result = dict(entry.result)
            result["rejected_attempts"] = list(entry.rejected_attempts)
            result["execution_id"] = entry.execution_id
            self._pending.pop(nonce, None)  # one-shot: consumed either way
        return result

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


class CheckinListener:
    """ONE Unix socket, ONE accept loop, serving every pending registration in a shared
    CheckinRegistry at once. Each accepted connection is handled on its own short-lived thread so
    one slow or malicious connection cannot block another concurrent delegation's check-in
    (lab/006). Linux-only (SO_PEERCRED) -- fails loudly at construction, never silently degrades."""

    def __init__(self, socket_path: str, registry: CheckinRegistry) -> None:
        if sys.platform != "linux":
            raise IdentityError(f"CheckinListener requires real Linux (SO_PEERCRED); detected sys.platform={sys.platform!r}")
        self.socket_path = socket_path
        self._registry = registry
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(socket_path)
        # bind() defaults to the broker's own umask permissions, which a provisioned unprivileged
        # uid cannot connect() through at all. Widening does not weaken verification -- the
        # nonce+SO_PEERCRED check below is the actual boundary and rejects anything that doesn't
        # match regardless of how the connection reached accept().
        os.chmod(socket_path, 0o777)
        self._sock.listen(32)
        self._sock.settimeout(0.5)
        self._stop = threading.Event()
        self._log_lock = threading.Lock()
        self.connections_handled: list[dict] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._sock.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    def __enter__(self) -> "CheckinListener":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        with conn:
            try:
                presented_nonce = conn.recv(4096).decode()
                peer_uid = read_peer_uid(conn)
            except OSError:
                return
            verified, matched_execution_id, reason = self._registry.handle_checkin(presented_nonce, peer_uid)
            with self._log_lock:
                self.connections_handled.append({
                    "peer_uid": peer_uid, "verified": verified,
                    "matched_execution_id": matched_execution_id, "reason": reason,
                })
            try:
                conn.sendall(b"1" if verified else b"0")
            except OSError:
                pass
