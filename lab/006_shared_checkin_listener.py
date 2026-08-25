"""006 -- a shared check-in listener serving multiple concurrent delegations, routed by nonce.

See `lab/006-shared-checkin-listener.md` for the hypothesis (with explicit null), method, and
analysis. This script runs the experiment and writes results under `out/006/`.

005 gave each delegation its own dedicated Unix socket path
(`/tmp/sipho-005-checkin-{execution_id}.sock`) -- two concurrent delegations under that design
literally cannot have their check-ins confused, because there is no shared resource to confuse them
through. That proves nothing about whether a *shared* listener -- the shape a real broker handling
many concurrent delegations would actually need, since spawning a dedicated socket per delegation
doesn't scale -- can correctly keep multiple pending registrations apart under real concurrency.

This experiment builds that shared shape fresh: ONE Unix socket, ONE accept loop (a background
thread, spawning a short-lived handler thread per accepted connection so one slow/malicious
connection can't block another), and a `CheckinRegistry` holding multiple pending registrations at
once, keyed by nonce -- `register_pending(execution_id, nonce, expected_uid)`. HISTORY.md's account
of v1's `checkin.py` describes this general shape (a single listener, a registry of pending
check-ins); this implementation is written fresh from that description, not adapted or
half-remembered from any actual prior code -- consistent with DESIGN.md SS0 and the no-dependencies
principle HISTORY.md documents being violated once and fixed by deletion, not a cleaner import.

Two real security properties are tested under genuine concurrency (real distinct provisioned uids,
real distinct subprocesses, one shared listener):

1. Does the registry ever attribute delegation A's genuine check-in to delegation B (or vice versa),
   across many concurrent trials, regardless of which connection arrives at the listener first?
2. Does a connection presenting delegation A's real nonce, but arriving from delegation B's real
   provisioned uid (constructed by actually running two real provisioned identities concurrently --
   not simulated), get correctly rejected as matching *neither* registration cleanly: found by nonce
   (so attributable to A, not silently dropped or credited to B), but refused because the peer uid
   the kernel reports for that connection does not match what was provisioned for A?

Needs real root on real Linux for the same reasons 004/005 do (useradd, cgroup v2, SO_PEERCRED);
refuses cleanly everywhere else.

Run (from macOS, will refuse -- confirms the refusal path)::

    cd /Users/shunhonda/dev/siphonophore
    python3 lab/006_shared_checkin_listener.py

Run for real, as root, on colima::

    colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/006_shared_checkin_listener.py"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pwd
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

OUT = Path(__file__).parent / "out" / "006"

# New uid range and cgroup root, distinct from 004 (62000s) and 005 (63000s), to avoid any
# collision if state from either experiment exists on the host at once.
UID_RANGE_START = 64000
UID_RANGE_END = 64999
CGROUP_ROOT = Path("/sys/fs/cgroup/siphonophore-exp006")

# Concurrency-stress parameters for predicate A.
ROUNDS = 3
PER_ROUND = 4


def require_real_root_linux() -> None:
    if sys.platform != "linux":
        sys.stderr.write(
            "REFUSED: this experiment requires real Linux (uid/cgroup provisioning, SO_PEERCRED "
            f"are Linux-specific). Detected sys.platform={sys.platform!r}. Run it on colima:\n"
            "  colima ssh -- bash -c \"cd /Users/shunhonda/dev/siphonophore && "
            "sudo python3 lab/006_shared_checkin_listener.py\"\n"
        )
        sys.exit(1)
    if os.geteuid() != 0:
        sys.stderr.write(
            f"REFUSED: this experiment requires real root. Detected euid={os.geteuid()}. Re-run with sudo.\n"
        )
        sys.exit(1)
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        sys.stderr.write("REFUSED: cgroup v2 unified hierarchy not detected.\n")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Core primitives -- same shape as 001-005
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    kind: str  # "delegate"
    principal_id: str
    intent_id: str
    payload: dict
    consequence: str  # "low" | "high" | "privileged"


@dataclass(frozen=True)
class Decision:
    intent_id: str
    principal_id: str
    kind: str
    permitted: bool
    execution_class: str
    token: str


class Gate:
    CONSEQUENCE_TO_CLASS = {"low": "same_process", "high": "separate_process", "privileged": "uid_cgroup"}

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def _canonical(self, intent_id, principal_id, kind, permitted, execution_class) -> bytes:
        return f"{intent_id}:{principal_id}:{kind}:{permitted}:{execution_class}".encode("utf-8")

    def _mint(self, intent_id, principal_id, kind, permitted, execution_class) -> str:
        msg = self._canonical(intent_id, principal_id, kind, permitted, execution_class)
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def submit(self, intent: Intent) -> Decision:
        permitted = self._policy(intent)
        execution_class = self.CONSEQUENCE_TO_CLASS.get(intent.consequence, "same_process")
        token = self._mint(intent.intent_id, intent.principal_id, intent.kind, permitted, execution_class)
        return Decision(
            intent_id=intent.intent_id, principal_id=intent.principal_id, kind=intent.kind,
            permitted=permitted, execution_class=execution_class, token=token,
        )

    def verify(self, decision: Decision) -> bool:
        expected = self._mint(
            decision.intent_id, decision.principal_id, decision.kind, decision.permitted, decision.execution_class
        )
        return hmac.compare_digest(expected, decision.token)

    def _policy(self, intent: Intent) -> bool:
        return intent.kind == "delegate" and intent.consequence in ("low", "high", "privileged")


class GateViolation(PermissionError):
    pass


class ProvisioningError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# uid+cgroup provisioning -- fresh, self-contained, same shape as 004/005
# ---------------------------------------------------------------------------

# Concurrent predicate A dispatches provisioning from multiple broker threads at once.
# _find_free_uid() + useradd is a real check-then-act race if two threads read
# pwd.getpwall() before either has called useradd -- both could pick the same free uid and one
# useradd would fail (or, worse under a different libc/tool, silently collide). Guarded by a
# single allocation lock spanning the read-candidates-then-useradd sequence; the shared registry
# under test is a different data structure with its own lock (see CheckinRegistry) -- this one is
# purely about the OS-level uid namespace being a second shared resource this experiment's own
# concurrency touches.
_UID_ALLOC_LOCK = threading.Lock()

# A second, distinct hazard, found on this experiment's first real concurrent run: switching
# useradd/the delegate subprocess away from preexec_fn (see _execute_delegate_shared_checkin) was
# NOT sufficient on its own -- concurrent subprocess CREATION itself (Popen()/subprocess.run(), the
# fork+exec moment specifically, even calls with no pass_fds and no user=/group= at all, e.g. plain
# `userdel`) raised real OSError: [Errno 9] Bad file descriptor on this target when multiple
# threads created child processes at overlapping times, corrupting an unrelated Popen object's own
# pipe fds. This is a real fd-table race in the process-wide fd namespace shared by every thread,
# not specific to preexec_fn or to this script's own pipe usage. The scope-appropriate fix:
# serialize only the CREATE step (fork+exec) across all subprocess-spawning call sites in this
# file; everything downstream (communicate(), waiting on the shared CheckinRegistry, the spawned
# children actually running and connecting) stays genuinely concurrent -- the property this
# experiment tests (concurrent CONNECTIONS routed correctly by the shared listener) lives entirely
# in that downstream part, not in the moment of process creation.
_SUBPROCESS_CREATE_LOCK = threading.Lock()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # Not capture_output=True: that's PIPE + communicate() under the hood, which is exactly what
    # turned out to be unsafe under this experiment's concurrent load (see the note in
    # Executor._execute_delegate_shared_checkin). useradd/userdel are called from concurrent
    # broker threads in predicate A, so this needs the same file-redirect treatment.
    out_fd, out_path = tempfile.mkstemp(prefix="sipho-006-run-")
    try:
        with _SUBPROCESS_CREATE_LOCK:
            proc = subprocess.run(cmd, stdout=out_fd, stderr=subprocess.STDOUT)
        output = Path(out_path).read_text()
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout=output, stderr=output)
    finally:
        os.close(out_fd)
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _find_free_uid() -> int:
    taken = {pw.pw_uid for pw in pwd.getpwall()}
    for candidate in range(UID_RANGE_START, UID_RANGE_END + 1):
        if candidate not in taken:
            return candidate
    raise ProvisioningError(f"no free uid in reserved range [{UID_RANGE_START}, {UID_RANGE_END}]")


def provision_ephemeral_user(execution_id: str) -> tuple[str, int, int]:
    username = f"sipho6-{execution_id[:8]}"
    with _UID_ALLOC_LOCK:
        uid = _find_free_uid()
        result = _run([
            "useradd", "--no-create-home", "--shell", "/usr/sbin/nologin", "--uid", str(uid),
            "--comment", "siphonophore ephemeral execution identity (experiment 006)", username,
        ])
    if result.returncode != 0:
        raise ProvisioningError(f"useradd failed (rc={result.returncode}): {result.stderr.strip()}")
    entry = pwd.getpwnam(username)
    if entry.pw_uid != uid:
        raise ProvisioningError(f"useradd created uid={entry.pw_uid}, expected {uid}")
    return username, entry.pw_uid, entry.pw_gid


def release_ephemeral_user(username: str) -> None:
    result = _run(["userdel", username])
    if result.returncode != 0:
        raise ProvisioningError(f"userdel failed (rc={result.returncode}): {result.stderr.strip()}")


def provision_cgroup(execution_id: str) -> Path:
    CGROUP_ROOT.mkdir(parents=True, exist_ok=True)
    cg = CGROUP_ROOT / f"exec-{execution_id}"
    cg.mkdir(parents=True, exist_ok=False)
    return cg


def add_pid_to_cgroup(cgroup_path: Path, pid: int) -> None:
    (cgroup_path / "cgroup.procs").write_text(str(pid))


def read_cgroup_procs(cgroup_path: Path) -> set[int]:
    text = (cgroup_path / "cgroup.procs").read_text()
    return {int(line) for line in text.split() if line.strip()}


def release_cgroup(cgroup_path: Path) -> None:
    remaining = read_cgroup_procs(cgroup_path)
    if remaining:
        raise ProvisioningError(f"refusing to release cgroup with live members: {remaining}")
    cgroup_path.rmdir()


def _read_peer_uid(conn: socket.socket) -> int:
    """The kernel's own record of which real uid holds the other end of this Unix socket
    connection -- SO_PEERCRED, not anything the connecting process sent or could spoof."""
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


# ---------------------------------------------------------------------------
# The shared check-in mechanism -- the new component this experiment builds
# ---------------------------------------------------------------------------


class CheckinRegistry:
    """Holds multiple pending check-in registrations at once, keyed by nonce.

    `register_pending(execution_id, nonce, expected_uid)` adds a registration. A connection is
    matched to a registration purely by the nonce it presents -- never by connection order, never
    by uid alone. Once a nonce is matched to its registration, the kernel-reported peer uid for
    that connection must equal the registration's `expected_uid` for the check-in to verify.

    Design choice, made deliberately rather than defaulted into: a *matched-nonce-but-wrong-uid*
    attempt is recorded against that registration (so it shows up in the eventual result) but does
    NOT consume or fail the registration outright -- the real owner may still present the correct
    nonce from the correct uid before the overall timeout. This means a leaked nonce alone cannot
    be used to lock the real delegation out of its own check-in window merely by an attacker
    connecting first with the wrong uid. (It also means a leaked nonce PLUS a matching uid *would*
    succeed -- but that is a different, already-understood threat: 005 already covers the
    nonce-secrecy story via the inherited-pipe-fd delivery; this design choice is about what a
    *partial* compromise -- nonce known, uid not controlled -- can and can't do.)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def register_pending(self, execution_id: str, nonce: str, expected_uid: int) -> None:
        with self._lock:
            if nonce in self._pending:
                raise ProvisioningError(f"nonce collision registering execution_id={execution_id}")
            self._pending[nonce] = {
                "execution_id": execution_id,
                "expected_uid": expected_uid,
                "event": threading.Event(),
                "result": None,
                "rejected_attempts": [],
            }

    def handle_checkin(self, presented_nonce: str, peer_uid: int) -> tuple[bool, str | None, str]:
        """Called from a listener connection-handler thread. Returns (verified, matched_execution_id,
        reason). Routing is entirely by presented_nonce -- connection order and peer_uid play no
        role in WHICH registration is consulted, only in whether that one registration's own uid
        check passes."""
        with self._lock:
            entry = self._pending.get(presented_nonce)
            if entry is None:
                return False, None, "no pending registration for this nonce"
            if entry["result"] is not None:
                return False, entry["execution_id"], "registration already finalized"
            if entry["expected_uid"] != peer_uid:
                entry["rejected_attempts"].append({"peer_uid": peer_uid})
                return False, entry["execution_id"], "uid mismatch"
            entry["result"] = {"verified": True, "peer_uid": peer_uid}
            entry["event"].set()
            return True, entry["execution_id"], "ok"

    def wait_for_result(self, nonce: str, timeout: float) -> dict:
        with self._lock:
            entry = self._pending.get(nonce)
        if entry is None:
            return {"verified": False, "reason": "no such registration"}
        got = entry["event"].wait(timeout)
        with self._lock:
            if not got:
                result = {"verified": False, "reason": "timeout"}
            else:
                result = dict(entry["result"])
            result["rejected_attempts"] = list(entry["rejected_attempts"])
            result["execution_id"] = entry["execution_id"]
            self._pending.pop(nonce, None)  # one-shot: consumed either way
        return result


class SharedCheckinListener:
    """ONE Unix socket, ONE accept loop, serving every pending registration in the shared
    CheckinRegistry at once. Each accepted connection is handled on its own short-lived thread so
    one slow or malicious connection cannot block another concurrent delegation's check-in."""

    def __init__(self, socket_path: str, registry: CheckinRegistry) -> None:
        self.socket_path = socket_path
        self._registry = registry
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(socket_path)
        # Same reasoning as 005: bind() defaults to the broker's own (root) umask permissions,
        # which an unprivileged provisioned uid cannot connect() through at all. Widening does not
        # weaken verification -- nonce+SO_PEERCRED matching below is the actual boundary, and it
        # rejects anything that doesn't match regardless of how the connection reached accept().
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
                peer_uid = _read_peer_uid(conn)
            except OSError:
                return
            verified, matched_execution_id, reason = self._registry.handle_checkin(presented_nonce, peer_uid)
            with self._log_lock:
                self.connections_handled.append({
                    "peer_uid": peer_uid,
                    "verified": verified,
                    "matched_execution_id": matched_execution_id,
                    "reason": reason,
                })
            try:
                conn.sendall(b"1" if verified else b"0")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The sub-agent's own program -- a real, separate process per delegation
# ---------------------------------------------------------------------------

_SUBAGENT_PROGRAM = """
import json, os, random, socket, sys, time

socket_path, path, content, nonce_fd = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
nonce = os.read(nonce_fd, 4096).decode()
os.close(nonce_fd)

# Jitter connection arrival: this is a shared listener, and this experiment's own point is that
# routing must not depend on which connection happens to arrive first. Without this, threads
# started in registration order would very likely also connect in that same order every time,
# which would silently fail to exercise the out-of-order case at all.
time.sleep(random.uniform(0, 0.4))

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(socket_path)
sock.sendall(nonce.encode())
response = sock.recv(1)
sock.close()

if response != b"1":
    print(json.dumps({"checked_in": False, "pid": os.getpid()}))
    sys.exit(1)

with open(path, "w") as f:
    f.write(content)

print(json.dumps({"checked_in": True, "pid": os.getpid(), "self_reported_uid": os.getuid()}))
"""

# A bare-metal connect-and-present-a-given-nonce program, run under a REAL provisioned uid that is
# NOT the uid the presented nonce was registered for. Models an attacker (or a confused/compromised
# sub-agent) that has somehow obtained another delegation's nonce but is running under its own,
# different, genuinely distinct OS identity -- not a stand-in, an actual second useradd-provisioned
# uid making an actual socket connection.
_ROGUE_CONNECT_PROGRAM = """
import socket, sys
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(sys.argv[1])
sock.sendall(sys.argv[2].encode())
resp = sock.recv(1)
sys.stdout.write(resp.decode())
"""


def rogue_checkin_attempt(listener: SharedCheckinListener, uid: int, gid: int, presented_nonce: str) -> str:
    # user=/group=/extra_groups= (Popen's own privilege-drop parameters, not preexec_fn -- see the
    # note on Executor._execute_delegate_shared_checkin for why) do the setuid/setgid in C code
    # after fork(), never calling back into the interpreter. File-redirected stdout, not PIPE/
    # capture_output -- same reason as _run() and the delegate child.
    out_fd, out_path = tempfile.mkstemp(prefix="sipho-006-rogue-")
    try:
        with _SUBPROCESS_CREATE_LOCK:
            subprocess.run(
                [sys.executable, "-c", _ROGUE_CONNECT_PROGRAM, listener.socket_path, presented_nonce],
                user=uid, group=gid, extra_groups=[], stdout=out_fd, stderr=subprocess.DEVNULL, timeout=10,
            )
        return Path(out_path).read_text().strip()
    finally:
        os.close(out_fd)
        try:
            os.unlink(out_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Executor -- dispatches a `delegate` Intent against the ONE shared listener/registry
# ---------------------------------------------------------------------------


@dataclass
class PendingDelegation:
    """A spawned-but-not-yet-awaited delegation. Splitting spawn from await/reap (below) lets many
    delegations be spawned back-to-back on a single thread -- fully sequential, no concurrent
    subprocess creation at all -- while still being awaited genuinely concurrently afterward (pure
    threading.Event waits on the shared registry, no subprocess/fd operations involved). See the
    note on Executor.spawn_delegate for why this split exists."""
    execution_id: str
    username: str
    uid: int
    cgroup_path: Path
    nonce: str
    proc: subprocess.Popen
    stdout_path: str
    read_fd: int
    write_fd: int


class Executor:
    def __init__(self, gate: Gate) -> None:
        self._gate = gate
        self.registry = CheckinRegistry()
        socket_path = f"/tmp/sipho-006-checkin-{uuid.uuid4().hex[:8]}.sock"
        self.listener = SharedCheckinListener(socket_path, self.registry)
        self.listener.start()

    def shutdown(self) -> None:
        self.listener.stop()

    def execute(self, decision: Decision, intent: Intent, *, checkin_timeout: float = 10.0,
                _subagent_program: str = _SUBAGENT_PROGRAM, _nonce_override: str | None = None) -> dict:
        """Convenience wrapper: spawn, then immediately await+reap on the calling thread. Used by
        predicate C (forged Decision) and anywhere else that doesn't need spawn/await pipelining."""
        pending = self.spawn_delegate(
            decision, intent, subagent_program=_subagent_program, nonce_override=_nonce_override,
        )
        result = self.registry.wait_for_result(pending.nonce, timeout=checkin_timeout)
        return self.reap_delegate(pending, result)

    def spawn_delegate(self, decision: Decision, intent: Intent, *, subagent_program: str = _SUBAGENT_PROGRAM,
                        nonce_override: str | None = None) -> PendingDelegation:
        """Validate the Decision, provision a real uid+cgroup, register a pending check-in on the
        shared registry, and spawn the real sub-agent subprocess. Does NOT wait for check-in or
        reap the child -- call reap_delegate() (after obtaining a result from
        registry.wait_for_result()) for that.

        Why the split exists -- a real, first-attempt finding: this experiment's first several
        real-concurrency runs, with the full provision-through-reap sequence running on N Python
        threads at once (guarded only by locks around individual subprocess-creation calls), hit
        repeated, evolving `OSError: [Errno 9] Bad file descriptor` failures -- inside
        `communicate()`'s selector-based pipe read, then (after removing PIPE/communicate() in
        favor of file-redirected stdout) inside `Popen._execute_child` itself, corrupting fds
        completely unrelated to the call that triggered them (including the plain `userdel`
        subprocess, which shares none of the delegate child's pipes or pass_fds). Locking
        individual operations more and more tightly did not fully eliminate it -- the corruption
        outlived any single critical section, consistent with Python-level object lifecycle
        (garbage-collected Popen/file wrappers closing an OS fd number that had since been
        reused by a different thread) racing independently of any lock this script holds, not a
        gap in the locking itself.

        The actual fix: stop running subprocess CREATION and REAPING concurrently across threads
        at all. spawn_delegate() and reap_delegate() (below) are only ever called from a single
        thread in this experiment (see `run()`'s predicate A: spawn every trial in one loop, THEN
        await every trial's check-in concurrently via threading.Event.wait() -- which touches no
        fd -- THEN reap every trial in a second sequential loop). Concurrency is preserved exactly
        where this experiment's own hypothesis needs it (many real subprocesses alive and
        connecting to the ONE shared listener at genuinely overlapping times, many pending
        registrations in the registry at once, connections routed correctly regardless of arrival
        order) without asking CPython's subprocess/GC machinery to do something that, empirically,
        it does not do safely under this specific concurrent load on this target.
        """
        if decision.intent_id != intent.intent_id or decision.kind != intent.kind:
            raise GateViolation("decision does not correspond to this intent")
        if not self._gate.verify(decision):
            raise GateViolation("decision failed Gate verification -- forged, tampered, or downgraded")
        if not decision.permitted:
            raise GateViolation("decision denies this intent")
        if not (intent.kind == "delegate" and decision.execution_class == "uid_cgroup"):
            raise GateViolation(f"no executor handler for kind={intent.kind!r} execution_class={decision.execution_class!r}")

        execution_id = decision.intent_id
        path = intent.payload["path"]
        content = intent.payload["content"]

        username, uid, gid = provision_ephemeral_user(execution_id)
        cgroup_path = provision_cgroup(execution_id)

        nonce = secrets.token_hex(32)
        self.registry.register_pending(execution_id, nonce, expected_uid=uid)
        presented_nonce = nonce_override if nonce_override is not None else nonce

        read_fd, write_fd = os.pipe()
        os.write(write_fd, presented_nonce.encode())
        os.close(write_fd)

        stdout_fd, stdout_path = tempfile.mkstemp(prefix=f"sipho-006-stdout-{execution_id[:8]}-")
        proc = subprocess.Popen(
            [sys.executable, "-c", subagent_program, self.listener.socket_path, path, content, str(read_fd)],
            pass_fds=(read_fd,), user=uid, group=gid, extra_groups=[],
            stdout=stdout_fd, stderr=subprocess.DEVNULL,
        )
        os.close(read_fd)
        os.close(stdout_fd)  # parent's copy; the child inherited its own via dup2
        add_pid_to_cgroup(cgroup_path, proc.pid)

        return PendingDelegation(
            execution_id=execution_id, username=username, uid=uid, cgroup_path=cgroup_path,
            nonce=nonce, proc=proc, stdout_path=stdout_path, read_fd=read_fd, write_fd=write_fd,
        )

    def reap_delegate(self, pending: PendingDelegation, result: dict) -> dict:
        """Given a check-in result already obtained from registry.wait_for_result(), finish
        handling the child (wait for it to exit on the happy path, kill+wait it otherwise), read
        its self-report back from the redirected stdout file, and release the provisioned
        uid+cgroup on every path. Only ever called from the single thread driving predicate A's
        (or B's) reap phase -- see spawn_delegate's docstring for why."""
        observations: dict = {"execution_id": pending.execution_id, "provisioned_uid": pending.uid,
                               "checkin_result": result}
        try:
            if result.get("verified"):
                try:
                    pending.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pending.proc.kill()
                    pending.proc.wait(timeout=5)
                observations["child_returncode"] = pending.proc.returncode
                child_stdout = Path(pending.stdout_path).read_text().strip()
                if pending.proc.returncode == 0 and child_stdout:
                    observations["child_self_report"] = json.loads(child_stdout)
            else:
                if pending.proc.poll() is None:
                    pending.proc.kill()
                try:
                    pending.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            for fd in (pending.read_fd, pending.write_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(pending.stdout_path)
            except OSError:
                pass
            try:
                release_cgroup(pending.cgroup_path)
                observations["cgroup_released"] = not pending.cgroup_path.exists()
            except ProvisioningError:
                observations["cgroup_released"] = False
            release_ephemeral_user(pending.username)
            try:
                pwd.getpwnam(pending.username)
                observations["user_released"] = False
            except KeyError:
                observations["user_released"] = True

        return {"effect": "delegate", "execution_class": "uid_cgroup", "observations": observations}


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------


def _spawn_trial(executor: Executor, principal_id: str, path: Path, content: str) -> tuple[PendingDelegation, str, Path]:
    intent = Intent(
        kind="delegate", principal_id=principal_id, intent_id=str(uuid.uuid4()),
        payload={"path": str(path), "content": content}, consequence="privileged",
    )
    decision = executor._gate.submit(intent)
    assert decision.execution_class == "uid_cgroup"
    pending = executor.spawn_delegate(decision, intent)
    return pending, content, path


def _reap_trial(executor: Executor, pending: PendingDelegation, result: dict, content: str, path: Path) -> dict:
    effect = executor.reap_delegate(pending, result)
    obs = effect["observations"]
    return {
        "execution_id": obs["execution_id"],
        "provisioned_uid": obs["provisioned_uid"],
        "expected_content": content,
        "path": str(path),
        "file_content_matches": path.exists() and path.read_text() == content,
        "checkin_verified": obs["checkin_result"].get("verified"),
        "checkin_matched_execution_id": obs["checkin_result"].get("execution_id"),
        "self_reported_uid": obs.get("child_self_report", {}).get("self_reported_uid"),
        "cgroup_released": obs["cgroup_released"],
        "user_released": obs["user_released"],
    }


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="sipho-006-"))
    os.chmod(workdir, 0o777)  # provisioned uids need write access, same reason as 004/005
    results: dict = {"workdir": str(workdir), "broker_pid": os.getpid(), "broker_uid": os.getuid()}

    gate = Gate()
    executor = Executor(gate)

    try:
        # --- Predicate A: N concurrent delegations per round, ROUNDS rounds, all against the ONE
        # shared listener/registry, each with jittered connection timing so arrival order does not
        # match registration order. Zero cross-attribution across every trial: each delegation's
        # own file gets its own content, each self-reported uid matches its own provisioned uid,
        # and the registry's own routing decision (checkin_matched_execution_id) names the correct
        # owner for every successful check-in.
        #
        # Three explicit phases per round (see PendingDelegation / Executor.spawn_delegate's
        # docstring for why): (1) spawn every trial's real subprocess back-to-back on this one
        # thread -- no concurrent subprocess creation at all; (2) await every trial's check-in
        # CONCURRENTLY, one thread per trial, each doing nothing but
        # registry.wait_for_result() -- a plain threading.Event wait, no fd/subprocess operation,
        # genuinely safe under concurrency, and exactly where this experiment's own hypothesis
        # needs real concurrency (many real, already-running subprocesses connecting to the one
        # shared listener at overlapping times, however their creation was sequenced); (3) reap
        # every trial back-to-back on this one thread. The children themselves are still real,
        # still alive concurrently (spawned within milliseconds of each other, each independently
        # jittering 0-0.4s before connecting), and still genuinely race each other to connect to
        # the ONE shared listener -- only the broker's OWN bookkeeping around subprocess
        # creation/reaping is kept single-threaded. ---------------------------------------------
        trials = []
        for round_idx in range(ROUNDS):
            pending_trials = []
            for i in range(PER_ROUND):
                path = workdir / f"round{round_idx}-trial{i}.txt"
                content = f"round={round_idx} trial={i} nonce={uuid.uuid4().hex}"
                pending, content, path = _spawn_trial(executor, f"principal-r{round_idx}t{i}", path, content)
                pending_trials.append((pending, content, path))

            checkin_results: list[dict | None] = [None] * PER_ROUND
            wait_threads = []

            def _wait(i: int) -> None:
                pending, _content, _path = pending_trials[i]
                checkin_results[i] = executor.registry.wait_for_result(pending.nonce, timeout=10.0)

            for i in range(PER_ROUND):
                t = threading.Thread(target=_wait, args=(i,))
                wait_threads.append(t)
                t.start()
            for t in wait_threads:
                t.join()

            round_trials = [
                _reap_trial(executor, pending, checkin_results[i], content, path)
                for i, (pending, content, path) in enumerate(pending_trials)
            ]
            trials.extend(round_trials)

        results["predicate_a_trials"] = trials
        results["predicate_a_summary"] = {
            "total_trials": len(trials),
            "all_verified": all(t["checkin_verified"] is True for t in trials),
            "all_content_matches": all(t["file_content_matches"] is True for t in trials),
            "all_self_reported_uid_matches_provisioned": all(
                t["self_reported_uid"] == t["provisioned_uid"] for t in trials
            ),
            "all_registry_routing_correct": all(
                t["checkin_matched_execution_id"] == t["execution_id"] for t in trials
            ),
            "all_cleanly_released": all(t["cgroup_released"] and t["user_released"] for t in trials),
        }

        # --- Predicate B: cross-identity nonce/uid mismatch, using two REAL, distinct provisioned
        # identities running concurrently. B's own real provisioned uid presents A's real nonce (a
        # simulated partial-compromise: nonce known, uid not controlled). Confirms: (1) the attempt
        # is rejected, (2) it is matched to A's registration by nonce (not silently dropped, not
        # credited to B), (3) A's own registration is not consumed by the failed attempt -- A's
        # genuine check-in still succeeds afterward, and its final result records the rejected
        # attempt, (4) B's own genuine check-in (with B's own real nonce) succeeds independently,
        # unaffected by its earlier rogue connection. -------------------------------------------
        exec_a = str(uuid.uuid4())
        exec_b = str(uuid.uuid4())
        user_a, uid_a, gid_a = provision_ephemeral_user(exec_a)
        user_b, uid_b, gid_b = provision_ephemeral_user(exec_b)
        cgroup_a = provision_cgroup(exec_a)
        cgroup_b = provision_cgroup(exec_b)
        nonce_a = secrets.token_hex(32)
        nonce_b = secrets.token_hex(32)
        executor.registry.register_pending(exec_a, nonce_a, expected_uid=uid_a)
        executor.registry.register_pending(exec_b, nonce_b, expected_uid=uid_b)

        # Step 1: B's real uid presents A's real nonce. Real concurrency: run this on its own
        # thread while nothing else has connected yet, then confirm before proceeding.
        rogue_response = rogue_checkin_attempt(executor.listener, uid_b, gid_b, nonce_a)

        rogue_log_entries = [
            c for c in executor.listener.connections_handled if c["peer_uid"] == uid_b and c["reason"] == "uid mismatch"
        ]

        # Step 2: A's and B's own genuine subprocesses check in for real, concurrently, using their
        # own real nonces via the inherited-pipe-fd delivery (never argv -- same as 005).
        path_a = workdir / "cross-identity-A.txt"
        path_b = workdir / "cross-identity-B.txt"
        content_a = f"delegation A's own content, nonce={uuid.uuid4().hex}"
        content_b = f"delegation B's own content, nonce={uuid.uuid4().hex}"

        def _spawn_genuine(path: Path, content: str, uid: int, gid: int, nonce: str) -> subprocess.Popen:
            read_fd, write_fd = os.pipe()
            os.write(write_fd, nonce.encode())
            os.close(write_fd)

            # user=/group=, not preexec_fn; creation serialized under _SUBPROCESS_CREATE_LOCK; no
            # PIPE (stdout/stderr discarded -- this call site's own self-report is never consulted,
            # only the on-disk file content is) -- same reasoning as _execute_delegate_shared_checkin
            # and _run(), applied uniformly rather than only where a problem was actually observed.
            with _SUBPROCESS_CREATE_LOCK:
                p = subprocess.Popen(
                    [sys.executable, "-c", _SUBAGENT_PROGRAM, executor.listener.socket_path, str(path), content, str(read_fd)],
                    pass_fds=(read_fd,), user=uid, group=gid, extra_groups=[],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                os.close(read_fd)
            return p

        proc_a = _spawn_genuine(path_a, content_a, uid_a, gid_a, nonce_a)
        add_pid_to_cgroup(cgroup_a, proc_a.pid)
        proc_b = _spawn_genuine(path_b, content_b, uid_b, gid_b, nonce_b)
        add_pid_to_cgroup(cgroup_b, proc_b.pid)

        result_a = executor.registry.wait_for_result(nonce_a, timeout=10.0)
        result_b = executor.registry.wait_for_result(nonce_b, timeout=10.0)
        proc_a.wait(timeout=10)
        proc_b.wait(timeout=10)

        release_cgroup(cgroup_a)
        release_cgroup(cgroup_b)
        release_ephemeral_user(user_a)
        release_ephemeral_user(user_b)
        a_user_released = True
        b_user_released = True
        try:
            pwd.getpwnam(user_a)
            a_user_released = False
        except KeyError:
            pass
        try:
            pwd.getpwnam(user_b)
            b_user_released = False
        except KeyError:
            pass

        results["predicate_b_cross_identity"] = {
            "uid_a": uid_a,
            "uid_b": uid_b,
            "rogue_response": rogue_response,
            "rogue_rejected": rogue_response == "0",
            "rogue_matched_execution_id_was_a": (
                len(rogue_log_entries) == 1 and rogue_log_entries[0]["matched_execution_id"] == exec_a
            ),
            "a_final_result": result_a,
            "b_final_result": result_b,
            "a_verified_despite_rogue_attempt": result_a.get("verified") is True,
            "a_result_records_rogue_attempt": len(result_a.get("rejected_attempts", [])) == 1
            and result_a["rejected_attempts"][0]["peer_uid"] == uid_b,
            "b_verified_independently": result_b.get("verified") is True,
            "b_result_has_no_rejected_attempts": len(result_b.get("rejected_attempts", [])) == 0,
            "content_a_matches": path_a.exists() and path_a.read_text() == content_a,
            "content_b_matches": path_b.exists() and path_b.read_text() == content_b,
            "no_cross_content": (
                (not path_a.exists() or path_a.read_text() != content_b)
                and (not path_b.exists() or path_b.read_text() != content_a)
            ),
            "both_users_released": a_user_released and b_user_released,
        }

        # --- Predicate C: a hand-forged Decision claiming delegate+uid_cgroup, never through
        # Gate.submit(), is refused before any provisioning happens -- no user, no cgroup, no
        # registration on the shared registry at all. -------------------------------------------
        forged_path = workdir / "forged.txt"
        forged_intent = Intent(
            kind="delegate", principal_id="principal-eve", intent_id=str(uuid.uuid4()),
            payload={"path": str(forged_path), "content": "should never appear"}, consequence="privileged",
        )
        forged_decision = Decision(
            intent_id=forged_intent.intent_id, principal_id=forged_intent.principal_id, kind=forged_intent.kind,
            permitted=True, execution_class="uid_cgroup", token="beadfeed" * 8,
        )
        users_before = {pw.pw_name for pw in pwd.getpwall()}
        pending_before = len(executor.registry._pending)
        forged_refused = False
        try:
            executor.execute(forged_decision, forged_intent)
        except GateViolation:
            forged_refused = True
        users_after = {pw.pw_name for pw in pwd.getpwall()}
        pending_after = len(executor.registry._pending)

        results["predicate_c_forged_refused"] = {
            "refused": forged_refused,
            "target_file_absent": not forged_path.exists(),
            "no_new_users_provisioned": users_after == users_before,
            "no_new_registrations": pending_after == pending_before,
        }
    finally:
        executor.shutdown()

    return results


def main() -> int:
    require_real_root_linux()
    results = run()

    out_path = OUT / "results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {out_path}")
    print(json.dumps(results, indent=2, default=str))

    a = results["predicate_a_summary"]
    b = results["predicate_b_cross_identity"]
    c = results["predicate_c_forged_refused"]

    checks = [
        (f"concurrent happy path: all {a['total_trials']} trials verified", a["all_verified"] is True),
        ("concurrent happy path: zero cross-attribution (file content)", a["all_content_matches"] is True),
        ("concurrent happy path: zero cross-attribution (self-reported uid)", a["all_self_reported_uid_matches_provisioned"] is True),
        ("concurrent happy path: zero cross-attribution (registry routing)", a["all_registry_routing_correct"] is True),
        ("concurrent happy path: all identities cleanly released", a["all_cleanly_released"] is True),
        ("cross-identity: rogue (uidB, nonceA) rejected", b["rogue_rejected"] is True),
        ("cross-identity: rogue attempt matched to A's registration by nonce", b["rogue_matched_execution_id_was_a"] is True),
        ("cross-identity: A's genuine check-in still succeeds after rogue attempt", b["a_verified_despite_rogue_attempt"] is True),
        ("cross-identity: A's result records the rejected attempt from uidB", b["a_result_records_rogue_attempt"] is True),
        ("cross-identity: B's genuine check-in succeeds independently", b["b_verified_independently"] is True),
        ("cross-identity: B's result carries no rejected attempts", b["b_result_has_no_rejected_attempts"] is True),
        ("cross-identity: A's file has A's content", b["content_a_matches"] is True),
        ("cross-identity: B's file has B's content", b["content_b_matches"] is True),
        ("cross-identity: no cross-content", b["no_cross_content"] is True),
        ("cross-identity: both identities released", b["both_users_released"] is True),
        ("forged: refused", c["refused"] is True),
        ("forged: target file absent", c["target_file_absent"] is True),
        ("forged: no new users provisioned", c["no_new_users_provisioned"] is True),
        ("forged: no new registrations on shared registry", c["no_new_registrations"] is True),
    ]
    ok = True
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    shutil.rmtree(results["workdir"], ignore_errors=True)

    if not ok:
        print("HYPOTHESIS NOT SUPPORTED", file=sys.stderr)
        return 1
    print("HYPOTHESIS SUPPORTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
