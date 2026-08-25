"""Tests for `siphonophore._severed_runner`, the entrypoint a severed node's spawned child process
actually runs.

None of this needs root: the runner's own job -- check in (client side only; it does not wait for
or care about verification, exactly like a real spawned node wouldn't), resolve+call its recipe,
and emit a result envelope on stdout -- has nothing to do with uid/cgroup provisioning. What it
deliberately does NOT prove is that the process was running under a *provisioned* uid inside a
*real* cgroup -- that's `test_orchestrator.py`'s `linux_root_only`-marked integration test's job.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile

from siphonophore import checkin
from siphonophore._severed_runner import main


def _socket_path() -> str:
    return os.path.join(tempfile.mkdtemp(), "severed-runner-test.sock")


def _nonce_read_fd(nonce: str) -> int:
    """Writes `nonce` into a fresh pipe and returns the read end's fd -- the same shape
    `Colony._dispatch_severed` hands the runner in production (see orchestrator.py), so these
    tests exercise the real argv/fd contract, not the old (fixed) nonce-in-argv one."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, nonce.encode())
    os.close(write_fd)
    return read_fd


def test_main_checks_in_runs_its_recipe_and_emits_a_result_envelope():
    """Calls the runner's own main() directly, in-process -- no subprocess, no uid switch -- to
    prove the check-in-then-run-then-emit sequence itself is correct."""
    path = _socket_path()
    server = checkin.CheckinServer(path)
    server.start()
    try:
        nonce = checkin.generate_nonce()
        server.register_pending("node-x", nonce, expected_uid=os.getuid())
        payload = json.dumps(
            {
                "factory": "siphonophore.testing:make_stub_agent",
                "kwargs": {"text": "from the runner"},
                "task": "hi",
                "invocation_state": {},
            }
        )

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main([path, str(_nonce_read_fd(nonce)), payload])
        finally:
            sys.stdout = old_stdout

        assert rc == 0
        envelope = json.loads(captured.getvalue().strip())
        assert envelope["agent_result"]["type"] == "agent_result"
        assert envelope["agent_result"]["message"]["content"][0]["text"] == "from the runner"
        assert envelope["usage"]["totalTokens"] == 2
        assert envelope["interrupts"] == []
    finally:
        server.stop()


def test_main_checks_in_even_when_nobody_registered_the_nonce():
    """The runner's own contract is "check in, then do the work" -- it doesn't (and structurally
    can't, since check_in() is one-way) know or care whether the broker's CheckinServer accepted
    it. An unregistered check-in must not prevent the runner from producing its result; whether an
    unverified severed node's result is ever trusted is the orchestrator's job (see
    Colony._await_checkin / CheckinTimeoutError), not this module's."""
    path = _socket_path()
    server = checkin.CheckinServer(path)
    server.start()
    try:
        nonce = checkin.generate_nonce()
        # deliberately never register_pending
        payload = json.dumps(
            {"factory": "siphonophore.testing:make_stub_agent", "kwargs": {}, "task": "hi", "invocation_state": {}}
        )
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = main([path, str(_nonce_read_fd(nonce)), payload])
        finally:
            sys.stdout = old_stdout
        assert rc == 0
        envelope = json.loads(captured.getvalue().strip())
        assert envelope["agent_result"]["type"] == "agent_result"
    finally:
        server.stop()


def test_runner_spawned_as_a_real_subprocess_round_trips_through_stdout():
    """One level closer to production than calling main() in-process: an actual
    `python -m siphonophore._severed_runner` subprocess, argv-driven exactly like
    `orchestrator.build_argv` constructs, spawned WITHOUT a uid switch (that half needs root -- see
    `test_orchestrator.py`'s linux_root_only test). Proves the module is correctly invocable as
    `python -m ...`, that argv parsing and stdout-as-the-result-channel work through a real process
    boundary, and that a real spawned process's check-in client call actually reaches the server."""

    async def run() -> None:
        path = _socket_path()
        server = checkin.CheckinServer(path)
        server.start()
        try:
            nonce = checkin.generate_nonce()
            server.register_pending("node-y", nonce, expected_uid=os.getuid())
            payload = json.dumps(
                {
                    "factory": "siphonophore.testing:make_stub_agent",
                    "kwargs": {"text": "from a real subprocess"},
                    "task": "hi",
                    "invocation_state": {},
                }
            )
            nonce_fd = _nonce_read_fd(nonce)
            argv = [sys.executable, "-m", "siphonophore._severed_runner", path, str(nonce_fd), payload]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(nonce_fd,),
            )
            os.close(nonce_fd)  # the child has its own inherited copy now
            stdout, stderr = await proc.communicate()
            assert proc.returncode == 0, stderr.decode(errors="replace")

            envelope = json.loads(stdout.decode().strip())
            assert envelope["agent_result"]["message"]["content"][0]["text"] == "from a real subprocess"

            deadline = asyncio.get_event_loop().time() + 2
            while asyncio.get_event_loop().time() < deadline and not server.is_verified("node-y"):
                await asyncio.sleep(0.01)
            if sys.platform == "linux":
                # SO_PEERCRED verification is Linux-only (see checkin.py) -- on other platforms the
                # check-in is sent but never verified, which is exactly right, not a bug to paper over.
                assert server.is_verified("node-y")
        finally:
            server.stop()

    asyncio.run(run())
