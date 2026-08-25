"""Phase 2 (interface only, per build-prompt.md): the per-node substrate-selection axis DESIGN.md
describes -- bare process+uid+cgroup / real container / microVM-or-gVisor -- expressed as an
interface a per-node choice could plug into, without changing the orchestrator's own dispatch
*sequence* (provision identity -> register nonce -> spawn -> add to cgroup -> await check-in ->
collect result -> release identity).

Only the bare-process tier is real. `ContainerSubstrate` and `MicroVMSubstrate` are honest stubs --
every method raises `NotImplementedError` naming itself, not a silent no-op and not a fake success
-- left for a separately-scoped pass, the same "explicitly out of scope, considered, not built"
discipline `warrant` used for real SPIRE deployment. See STATUS.md for why this repo stops here for
now, and note: `Colony._dispatch_severed` does NOT currently route through this interface -- it
still does the bare-process work inline, exactly as validated by Phase 1's tests. Wiring Phase 1's
already-green dispatch through this interface for a single tier with no behavior change is left for
whoever builds the second tier, so it can be validated once, together with an actual reason to
abstract over more than one implementation.
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from typing import Any

from strands.agent.agent_result import AgentResult
from strands.interrupt import Interrupt
from strands.types.event_loop import Metrics, Usage

from .identity import Identity


@dataclass(frozen=True)
class SubstrateResult:
    """What any substrate tier hands back to the orchestrator once a severed node's work is done
    -- the same shape `Colony._dispatch_severed` builds inline today for the bare-process tier,
    pulled out here so a different substrate could produce it its own way (a container runtime's
    captured logs instead of stdout, a microVM's vsock instead of a pipe) without the orchestrator
    itself needing to know which."""

    agent_result: AgentResult
    usage: Usage
    metrics: Metrics
    interrupts: list[Interrupt]


class Substrate(abc.ABC):
    """A per-node choice of *how* a severed node's process boundary is actually implemented. Not
    yet consumed by `Colony` (see module docstring) -- this is the shape a future
    `add_severed_node(..., substrate=...)` parameter would accept."""

    @abc.abstractmethod
    async def spawn(self, identity: Identity, argv: list[str]) -> Any:
        """Starts the node's work under this substrate, given its already-provisioned `Identity`
        and the argv a bare-process tier would exec directly (`orchestrator.build_argv`'s output).
        Returns an opaque handle passed to `collect`/`teardown` -- an `asyncio.subprocess.Process`
        for the bare-process tier; a container/VM handle for the heavier tiers."""

    @abc.abstractmethod
    async def collect(self, handle: Any) -> SubstrateResult:
        """Blocks until the node's work finishes and returns its result. Raises on any failure --
        never returns a fabricated/partial `SubstrateResult` for work that didn't actually finish."""

    @abc.abstractmethod
    async def teardown(self, handle: Any) -> None:
        """Best-effort cleanup of whatever `spawn` started. Called whether or not `collect`
        succeeded, mirroring `Colony._dispatch_severed`'s own finally-block discipline -- a
        substrate's teardown is not optional just because collection already failed."""


class ProcessSubstrate(Substrate):
    """The one tier that's actually real: bare process + uid + cgroup, inside a shared outer
    container -- cheap, no VM/container boot latency per delegation, per DESIGN.md's "sufficient
    for narrow-authority/trusted-input nodes" tier. Functionally identical to what
    `Colony._dispatch_severed` already does inline; exists to prove this interface shape actually
    fits the one tier that's built, before either of the two unbuilt tiers is asked to fit it too.
    """

    async def spawn(self, identity: Identity, argv: list[str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *argv,
            user=identity.uid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def collect(self, handle: asyncio.subprocess.Process) -> SubstrateResult:
        from .orchestrator import parse_runner_output  # local import: avoids a substrate<->orchestrator cycle

        stdout, stderr = await handle.communicate()
        if handle.returncode != 0:
            raise RuntimeError(
                f"severed node process exited {handle.returncode}: {stderr.decode('utf-8', errors='replace')[-4000:]}"
            )
        agent_result, usage, metrics, interrupts = parse_runner_output("<process-substrate>", stdout)
        return SubstrateResult(agent_result=agent_result, usage=usage, metrics=metrics, interrupts=interrupts)

    async def teardown(self, handle: asyncio.subprocess.Process) -> None:
        if handle.returncode is None:
            handle.kill()
            await handle.wait()


class ContainerSubstrate(Substrate):
    """Full namespace isolation (pid/mount/network) -- worth the spin-up cost specifically for
    nodes handling untrusted binary parsing or broad/consequential authority, per DESIGN.md's
    substrate-selection axes. NOT IMPLEMENTED: a real backend (e.g. runc-driven, in the spirit of
    issue #2830's `BubblewrapSandbox`) is a separately-scoped pass. Every method here raises
    naming itself -- never a silent no-op, never a fake container."""

    async def spawn(self, identity: Identity, argv: list[str]) -> Any:
        raise NotImplementedError("ContainerSubstrate.spawn is an interface stub, not built -- see STATUS.md")

    async def collect(self, handle: Any) -> SubstrateResult:
        raise NotImplementedError("ContainerSubstrate.collect is an interface stub, not built -- see STATUS.md")

    async def teardown(self, handle: Any) -> None:
        raise NotImplementedError("ContainerSubstrate.teardown is an interface stub, not built -- see STATUS.md")


class MicroVMSubstrate(Substrate):
    """Firecracker microVM, or a syscall-interception sandbox such as gVisor -- the heaviest tier,
    for the highest-risk quadrant (broad authority AND untrusted-input exposure), not a default.
    NOT IMPLEMENTED -- see STATUS.md."""

    async def spawn(self, identity: Identity, argv: list[str]) -> Any:
        raise NotImplementedError("MicroVMSubstrate.spawn is an interface stub, not built -- see STATUS.md")

    async def collect(self, handle: Any) -> SubstrateResult:
        raise NotImplementedError("MicroVMSubstrate.collect is an interface stub, not built -- see STATUS.md")

    async def teardown(self, handle: Any) -> None:
        raise NotImplementedError("MicroVMSubstrate.teardown is an interface stub, not built -- see STATUS.md")
