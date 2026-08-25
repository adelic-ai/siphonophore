"""Tests for `siphonophore.substrate` (Phase 2, interface only -- see STATUS.md).

`ProcessSubstrate` is exercised for real: `spawn` is given a plain `Identity` carrying this test
process's own real uid (never fabricated -- it's `os.getuid()`, just packaged into the dataclass
`identity.provision_identity` would otherwise build), so `user=identity.uid` is a real, permitted
no-op uid-set rather than a privileged uid switch. That's enough to prove the interface's
spawn/collect/teardown shape actually works end to end for the one tier that's built, without
needing root.

`ContainerSubstrate`/`MicroVMSubstrate` are checked to do exactly what they claim: raise
`NotImplementedError` naming themselves, not silently succeed.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from siphonophore import checkin
from siphonophore.identity import Identity
from siphonophore.orchestrator import build_argv, SeveredRecipe
from siphonophore.substrate import ContainerSubstrate, MicroVMSubstrate, ProcessSubstrate


def _self_identity(node_id: str = "test-node") -> Identity:
    return Identity(
        node_id=node_id,
        uid=os.getuid(),
        username="not-a-real-provisioned-user",
        cgroup_name=node_id,
        cgroup_path="/nonexistent",  # ProcessSubstrate never touches the cgroup itself
    )


def test_process_substrate_spawns_collects_and_tears_down_for_real():
    async def run() -> None:
        socket_path = "/tmp/siphonophore-substrate-test.sock"
        server = checkin.CheckinServer(socket_path)
        server.start()
        try:
            nonce = checkin.generate_nonce()
            server.register_pending("test-node", nonce, expected_uid=os.getuid())
            read_fd, write_fd = os.pipe()
            os.write(write_fd, nonce.encode())
            os.close(write_fd)
            argv = build_argv(
                socket_path,
                read_fd,
                SeveredRecipe(factory="siphonophore.testing:make_stub_agent", kwargs={"text": "via substrate"}),
                "hi",
                {},
            )

            substrate = ProcessSubstrate()
            identity = _self_identity()
            handle = await substrate.spawn(identity, argv, pass_fds=(read_fd,))
            os.close(read_fd)  # the child has its own inherited copy now
            try:
                result = await substrate.collect(handle)
            finally:
                await substrate.teardown(handle)

            assert str(result.agent_result).strip() == "via substrate"
            assert result.usage["totalTokens"] == 2
        finally:
            server.stop()

    asyncio.run(run())


def test_process_substrate_collect_raises_on_a_nonzero_exit():
    async def run() -> None:
        substrate = ProcessSubstrate()
        identity = _self_identity()
        # A real spawn that deliberately exits nonzero -- not a mock, a real subprocess.
        handle = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "exit 7", user=identity.uid, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            with pytest.raises(RuntimeError):
                await substrate.collect(handle)
        finally:
            await substrate.teardown(handle)

    asyncio.run(run())


def test_process_substrate_teardown_kills_a_still_running_process():
    async def run() -> None:
        substrate = ProcessSubstrate()
        identity = _self_identity()
        handle = await asyncio.create_subprocess_exec(
            "/bin/sleep", "30", user=identity.uid, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        assert handle.returncode is None
        await substrate.teardown(handle)
        await asyncio.wait_for(handle.wait(), timeout=5)
        assert handle.returncode is not None  # actually terminated, not left running

    asyncio.run(run())


@pytest.mark.parametrize("substrate_cls", [ContainerSubstrate, MicroVMSubstrate])
def test_unbuilt_substrate_tiers_raise_rather_than_fake_success(substrate_cls):
    async def run() -> None:
        substrate = substrate_cls()
        identity = _self_identity()
        with pytest.raises(NotImplementedError):
            await substrate.spawn(identity, ["irrelevant"])

    asyncio.run(run())
