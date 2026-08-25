"""`Colony` -- a `strands.multiagent.base.MultiAgentBase` implementation whose per-node dispatch
decides, at registration time, whether a node stays part of the shared process or is severed into
its own OS-provisioned process. See DESIGN.md's octopus/colony framing for why most nodes should
stay non-severed, and STATUS.md for the resolution of this module's one open design question:
severed nodes take a picklable *recipe*, never a live `Agent`.

Non-severed dispatch changes nothing about how Strands itself works: `node.executor.stream_async`
is called in-process, exactly like `Swarm._execute_node` / `Graph._execute_node` do today (checked
against the installed package, not assumed -- see DESIGN.md).

Severed dispatch wires together the two already-built, already-validated primitives:

    1. `identity.provision_identity(node_id)` -- a real uid + cgroup, before anything is spawned.
    2. A nonce (`checkin.generate_nonce()`) registered with a `CheckinServer` before spawn, written
       into one end of a fresh pipe whose *other* end (the read fd) is passed to the child via
       `pass_fds` -- not argv, not an env var. See `build_argv`'s docstring for why: checked
       directly, `/proc/<pid>/cmdline` is world-readable on real Linux, which argv would have
       exposed the nonce through for the child's entire lifetime.
    3. `asyncio.create_subprocess_exec(..., user=<provisioned uid>, pass_fds=(nonce_read_fd,))`.
    4. `identity.add_pid_to_cgroup(...)` for the spawned pid.
    5. The child's first action, before touching its own recipe or task: `checkin.check_in(...)`
       (see `_severed_runner.py`).
    6. This orchestrator blocks, with a real timeout, on `CheckinServer.is_verified(node_id)`
       before treating the node as trustworthy enough to have dispatched it anything.
    7. `identity.release_identity(...)` once the node's result is collected -- cgroup first, then
       uid, per `release_identity`'s own ordering.

Steps 1-2 and 4-7 need real root on real Linux to run for real (useradd, cgroupfs) and are only
exercised by this repo's `linux_root_only`-marked tests -- see STATUS.md for what's been validated
here versus what's left for a human to run on a real Linux host.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from strands.agent import Agent
from strands.agent.agent_result import AgentResult
from strands.interrupt import Interrupt
from strands.multiagent.base import MultiAgentBase, MultiAgentResult, NodeResult, Status
from strands.types.event_loop import Metrics, Usage
from strands.types.multiagent import MultiAgentInput

from . import checkin, identity

#: How long an orchestrator waits for a severed node's process to check in before giving up on it
#: as untrusted. A node past this window is a dispatch failure, never a hang and never implicitly
#: trusted just because it was provisioned -- see checkin.py's own docstring on why provisioning
#: alone is not trust.
DEFAULT_CHECKIN_TIMEOUT = 30.0

#: How often `_await_checkin` polls `CheckinServer.is_verified` while waiting. Plain polling, not
#: an event/callback handoff across the server's own accept-loop thread -- simpler to reason about
#: correctly than cross-thread signaling, and 20ms is well under any timeout this module expects
#: to be configured with.
_CHECKIN_POLL_INTERVAL = 0.02


class OrchestratorError(RuntimeError):
    """Base class for errors raised by Colony's own dispatch logic, distinct from an underlying
    node's own Agent raising (which becomes that node's FAILED NodeResult, not an orchestrator
    error)."""


class DuplicateNodeError(OrchestratorError):
    """Two nodes were registered under the same node_id."""


class UnknownNodeError(OrchestratorError):
    """invoke_async was asked to dispatch a node_id nothing registered."""


class RecipeError(OrchestratorError):
    """A severed node's recipe reference doesn't resolve to a module-level, importable callable --
    raised at registration time (see `add_severed_node`), not at dispatch time inside a child
    process that has no clean way to report a resolution failure back."""


class CheckinTimeoutError(OrchestratorError):
    """A severed node's spawned process never checked in within the configured timeout. Distinct
    from `checkin.CheckinError` (a check-in that arrived but failed verification) -- this is a
    check-in that never arrived at all."""


@dataclass(frozen=True)
class SeveredRecipe:
    """A picklable-*by-reference* recipe for building a severed node's `Agent` -- built fresh
    *inside* the node's own spawned child process, not constructed here and sent across the
    boundary. See STATUS.md for the full reasoning behind this over sending a live `Agent`.

    `factory` is a "module:qualname" reference to a module-level callable -- not a lambda, not a
    closure, not a bound method -- the same constraint Python's own multiprocessing puts on
    picklable callables, enforced here by import (see `resolve_factory`), not by attempting to
    pickle anything, since nothing about the `Agent` itself is ever pickled: the child re-imports
    and re-calls the factory fresh, inside its own process.

    `kwargs` is passed to the factory as `factory(**kwargs)` and must itself be JSON-serializable
    -- it crosses the process boundary as part of an argv-carried JSON payload, not an env var.
    """

    factory: str
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _NonSeveredNode:
    node_id: str
    executor: Agent


@dataclass
class _SeveredNode:
    node_id: str
    recipe: SeveredRecipe
    checkin_timeout: float


def resolve_factory(reference: str) -> Callable[..., Agent]:
    """Imports and returns the callable a `"module:qualname"` recipe reference points at.

    Raises `RecipeError` -- not a bare `ImportError`/`AttributeError`/`ValueError` -- so a bad
    recipe fails with a message that names the actual problem, whether at registration time (the
    orchestrator's own fail-fast check) or inside a severed child re-resolving the same reference
    fresh.
    """
    if ":" not in reference:
        raise RecipeError(
            f"recipe factory {reference!r} must be 'module:qualname', e.g. 'pkg.mod:make_agent'"
        )
    module_name, _, qualname = reference.partition(":")
    if not module_name or not qualname:
        raise RecipeError(f"recipe factory {reference!r} must be 'module:qualname', both non-empty")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RecipeError(f"recipe factory {reference!r}: module {module_name!r} is not importable: {exc}") from exc

    obj: Any = module
    for part in qualname.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError as exc:
            raise RecipeError(
                f"recipe factory {reference!r}: {qualname!r} not found in module {module_name!r}"
            ) from exc

    if not callable(obj):
        raise RecipeError(f"recipe factory {reference!r} resolves to a non-callable: {obj!r}")

    # A closure or lambda re-imports to a *different* function object with an unresolvable
    # enclosing scope in a fresh child process -- catch this at registration time, not as a
    # mysterious NameError/AttributeError inside a spawned child that has no clean way to report
    # it back to the broker that provisioned it.
    qual = getattr(obj, "__qualname__", "")
    if "<locals>" in qual:
        raise RecipeError(
            f"recipe factory {reference!r} is a closure/local function ({qual!r}) -- a severed "
            "node's child process re-imports the module fresh and cannot see a function's own "
            "enclosing scope, so the factory must be defined at module level"
        )
    return obj


def build_argv(
    socket_path: str, nonce_fd: int, recipe: SeveredRecipe, task: MultiAgentInput, invocation_state: dict[str, Any]
) -> list[str]:
    """The argv a severed node's child process is spawned with. The recipe reference and task
    travel here, in argv -- neither is a secret. The nonce deliberately does NOT: it travels via
    an inherited pipe file descriptor instead (`nonce_fd`, the read end, passed via
    `pass_fds` at spawn time -- see `_dispatch_severed`), because checked directly against a real
    Linux host, `/proc/<pid>/cmdline` is world-readable (mode 0444) for that process's entire
    lifetime -- any local process, any uid, could read a nonce placed in argv, not just briefly at
    exec time. (`/proc/<pid>/environ` is actually the *more* protected of the two, owner-uid-only
    by default -- the opposite of what an earlier version of this code assumed without checking. A
    pipe fd beats both: nothing outside the two ends of that specific pipe can read it at all.)"""
    payload = {
        "factory": recipe.factory,
        "kwargs": recipe.kwargs,
        "task": task,
        "invocation_state": invocation_state,
    }
    return [
        sys.executable, "-m", "siphonophore._severed_runner", socket_path, str(nonce_fd), json.dumps(payload),
    ]


def parse_runner_output(node_id: str, stdout: bytes) -> tuple[AgentResult, Usage, Metrics, list[Interrupt]]:
    """Parses a severed node's stdout back into an `AgentResult` plus the usage/metrics/interrupts
    that traveled alongside it.

    Why not just `AgentResult.from_dict(json.loads(stdout))`: checked against the installed
    package -- `AgentResult.to_dict()`/`from_dict()` deliberately only round-trip
    message/stop_reason/checkpoint (built for session persistence, not full-fidelity IPC);
    `from_dict` always reconstructs a fresh, empty `EventLoopMetrics()`. A severed node's real
    token usage and latency would silently zero out crossing the process boundary if this used
    that round-trip alone -- so `_severed_runner.py` sends a small envelope carrying usage/metrics/
    interrupts as their own top-level JSON fields, alongside (not instead of) the standard
    `agent_result.to_dict()` payload. See STATUS.md.
    """
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise OrchestratorError(f"severed node {node_id!r} produced no output on stdout")
    last_line = text.splitlines()[-1]
    try:
        envelope = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise OrchestratorError(f"severed node {node_id!r} produced unparseable output: {exc}") from exc

    if not isinstance(envelope, dict) or "agent_result" not in envelope:
        raise OrchestratorError(f"severed node {node_id!r} produced a malformed result envelope: {envelope!r}")

    agent_result = AgentResult.from_dict(envelope["agent_result"])
    usage = Usage(**envelope.get("usage", {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}))
    metrics = Metrics(**envelope.get("metrics", {"latencyMs": 0}))
    interrupts = [Interrupt(**d) for d in envelope.get("interrupts", [])]
    return agent_result, usage, metrics, interrupts


class Colony(MultiAgentBase):
    """A `MultiAgentBase` orchestrator whose nodes are each, individually, either non-severed
    (dispatched in-process, the Strands default) or severed (dispatched in their own
    OS-provisioned process). See the module docstring for the full severed-dispatch pipeline.

    `invoke_async` fans out to every registered node (or a caller-selected subset, via
    `invocation_state={"nodes": [...]}`) concurrently against the same task, and merges their
    results into a single `MultiAgentResult`. Deliberately not a handoff engine (`Swarm`) or a
    dependency DAG (`Graph`) -- Phase 1's scope is the node-dispatch mechanism itself (severed vs.
    non-severed), not a new sequencing algorithm; see STATUS.md.
    """

    def __init__(self, *, id: str | None = None, checkin_socket_path: str | None = None) -> None:
        super().__init__()
        self.id = id or f"colony-{uuid.uuid4().hex[:8]}"
        self._nonsevered: dict[str, _NonSeveredNode] = {}
        self._severed: dict[str, _SeveredNode] = {}
        self._checkin_socket_path = checkin_socket_path or f"/tmp/siphonophore-{self.id}.sock"
        self._checkin_server: checkin.CheckinServer | None = None

    @property
    def node_ids(self) -> list[str]:
        """Every registered node_id, severed and non-severed, in registration order."""
        return [*self._nonsevered, *self._severed]

    def _check_new_node_id(self, node_id: str) -> None:
        if node_id in self._nonsevered or node_id in self._severed:
            raise DuplicateNodeError(f"node {node_id!r} is already registered")

    def add_node(self, node_id: str, executor: Agent) -> None:
        """Register a non-severed node -- stays part of the shared process. Dispatched exactly
        like Strands' own `Swarm`/`Graph` dispatch a node today: `executor.stream_async(...)`
        in-process, no OS-level change in behavior, no root required."""
        self._check_new_node_id(node_id)
        self._nonsevered[node_id] = _NonSeveredNode(node_id=node_id, executor=executor)

    def add_severed_node(
        self,
        node_id: str,
        recipe: SeveredRecipe,
        *,
        checkin_timeout: float = DEFAULT_CHECKIN_TIMEOUT,
    ) -> None:
        """Register a severed node -- provisioned a real uid + cgroup and dispatched inside its own
        OS process at invocation time. `recipe.factory` is resolved (imported) right now, at
        registration time, so a bad reference fails loudly before any process is ever spawned."""
        self._check_new_node_id(node_id)
        resolve_factory(recipe.factory)  # fail fast; import-check only, never invoked here
        self._severed[node_id] = _SeveredNode(node_id=node_id, recipe=recipe, checkin_timeout=checkin_timeout)

    async def _await_checkin(self, node_id: str, timeout: float) -> bool:
        """Blocks, with a real deadline, until `node_id` checks in or `timeout` elapses. Plain
        polling against the (already thread-safe) `CheckinServer` -- correct and simple to reason
        about, at the cost of up to one poll interval of extra latency, which is negligible next
        to any timeout this is realistically configured with."""
        assert self._checkin_server is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._checkin_server.is_verified(node_id):
                return True
            await asyncio.sleep(_CHECKIN_POLL_INTERVAL)
        return self._checkin_server.is_verified(node_id)

    async def _dispatch_nonsevered(
        self, node: _NonSeveredNode, task: MultiAgentInput, invocation_state: dict[str, Any]
    ) -> NodeResult:
        """In-process dispatch, unchanged from what Strands' own Swarm/Graph do today."""
        start_time = time.time()
        result: AgentResult | None = None
        async for event in node.executor.stream_async(task, invocation_state=invocation_state):
            if "result" in event:
                result = event["result"]
        if result is None:
            raise OrchestratorError(f"node {node.node_id!r} did not produce a result event")

        execution_time = round((time.time() - start_time) * 1000)
        status = Status.INTERRUPTED if result.stop_reason == "interrupt" else Status.COMPLETED
        result_metrics = getattr(result, "metrics", None)
        usage = getattr(result_metrics, "accumulated_usage", Usage(inputTokens=0, outputTokens=0, totalTokens=0))
        metrics = getattr(result_metrics, "accumulated_metrics", Metrics(latencyMs=execution_time))
        return NodeResult(
            result=result,
            execution_time=execution_time,
            status=status,
            accumulated_usage=usage,
            accumulated_metrics=metrics,
            execution_count=1,
            interrupts=list(result.interrupts or []),
        )

    async def _dispatch_severed(
        self, node: _SeveredNode, task: MultiAgentInput, invocation_state: dict[str, Any]
    ) -> NodeResult:
        """The full severed-node pipeline -- see the module docstring for the numbered steps this
        implements. Needs real root on real Linux to succeed (identity.provision_identity); on any
        other platform this raises for real (useradd/cgroupfs failures propagate, nothing here
        fakes or swallows them) rather than pretending to have provisioned anything."""
        start_time = time.time()
        node_id = node.node_id

        # 1. Provision a real uid + cgroup before spawning anything.
        ident = identity.provision_identity(node_id)
        try:
            # 2. Register a nonce for this node before it can possibly connect.
            nonce = checkin.generate_nonce()
            assert self._checkin_server is not None
            self._checkin_server.register_pending(node_id, nonce, expected_uid=ident.uid)

            proc: asyncio.subprocess.Process | None = None
            nonce_read_fd, nonce_write_fd = os.pipe()
            try:
                # The nonce travels to the child via this pipe's read end, inherited at spawn --
                # not argv (world-readable via /proc/<pid>/cmdline on real Linux, confirmed
                # directly, not assumed -- see build_argv's docstring) and not an env var either.
                os.write(nonce_write_fd, nonce.encode())
                os.close(nonce_write_fd)
                nonce_write_fd = -1  # already closed; skip the finally-block close below

                # 3. Spawn the child under the provisioned uid; only the nonce fd and recipe
                # reference travel with it -- the recipe/task themselves aren't secrets.
                argv = build_argv(self._checkin_socket_path, nonce_read_fd, node.recipe, task, invocation_state)
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    user=ident.uid,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    pass_fds=(nonce_read_fd,),
                )
                # The child has its own independent copy of the read end now (duplicated across
                # the fork/exec by pass_fds) -- this process's copy is no longer needed. Closing it
                # does not affect the child's copy.
                os.close(nonce_read_fd)
                nonce_read_fd = -1

                # 4. Track the whole descendant subtree via cgroup, not just this one pid.
                identity.add_pid_to_cgroup(ident.cgroup_path, proc.pid)

                # 5/6. Block on a real, proxied check-in before trusting this node with anything.
                verified = await self._await_checkin(node_id, node.checkin_timeout)
                if not verified:
                    raise CheckinTimeoutError(
                        f"severed node {node_id!r} (pid {proc.pid}, uid {ident.uid}) did not check "
                        f"in within {node.checkin_timeout}s -- treated as untrusted, never dispatched"
                    )

                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise OrchestratorError(
                        f"severed node {node_id!r} exited {proc.returncode}: "
                        f"{stderr.decode('utf-8', errors='replace')[-4000:]}"
                    )
                agent_result, usage, metrics, interrupts = parse_runner_output(node_id, stdout)
            finally:
                # Safe-close whichever end(s) of the nonce pipe are still open -- both start real,
                # both get set to -1 the moment this code itself closes them, so this never
                # double-closes a fd on the happy path and still cleans up on any early exception.
                for fd in (nonce_read_fd, nonce_write_fd):
                    if fd != -1:
                        os.close(fd)
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        finally:
            # 7. Release cgroup first, then uid, per release_identity's own ordering.
            identity.release_identity(ident)

        execution_time = round((time.time() - start_time) * 1000)
        status = Status.INTERRUPTED if agent_result.stop_reason == "interrupt" else Status.COMPLETED
        return NodeResult(
            result=agent_result,
            execution_time=execution_time,
            status=status,
            accumulated_usage=usage,
            accumulated_metrics=metrics,
            execution_count=1,
            interrupts=interrupts,
        )

    async def invoke_async(
        self, task: MultiAgentInput, invocation_state: dict[str, Any] | None = None, **kwargs: Any
    ) -> MultiAgentResult:
        invocation_state = dict(invocation_state or {})
        invocation_state.update(kwargs)
        requested = invocation_state.pop("nodes", None)
        node_ids = list(requested) if requested is not None else self.node_ids

        if not node_ids:
            raise OrchestratorError("no nodes registered to dispatch")
        unknown = [nid for nid in node_ids if nid not in self._nonsevered and nid not in self._severed]
        if unknown:
            raise UnknownNodeError(f"invoke_async requested unknown node(s): {unknown!r}")

        self._invocation_start_time = time.time()

        severed_requested = [nid for nid in node_ids if nid in self._severed]
        started_server = False
        if severed_requested and self._checkin_server is None:
            self._checkin_server = checkin.CheckinServer(self._checkin_socket_path)
            self._checkin_server.start()
            started_server = True

        try:
            coros = []
            for nid in node_ids:
                if nid in self._nonsevered:
                    coros.append(self._dispatch_nonsevered(self._nonsevered[nid], task, invocation_state))
                else:
                    coros.append(self._dispatch_severed(self._severed[nid], task, invocation_state))
            outcomes = await asyncio.gather(*coros, return_exceptions=True)
        finally:
            if started_server and self._checkin_server is not None:
                self._checkin_server.stop()
                self._checkin_server = None

        results: dict[str, NodeResult] = {}
        overall_status = Status.COMPLETED
        total_usage = Usage(inputTokens=0, outputTokens=0, totalTokens=0)
        total_metrics = Metrics(latencyMs=0)
        execution_count = 0
        for nid, outcome in zip(node_ids, outcomes):
            if isinstance(outcome, BaseException):
                node_result = NodeResult(result=outcome, status=Status.FAILED, execution_count=1)
            else:
                node_result = outcome
            results[nid] = node_result
            if node_result.status == Status.FAILED:
                overall_status = Status.FAILED
            elif node_result.status == Status.INTERRUPTED and overall_status != Status.FAILED:
                overall_status = Status.INTERRUPTED
            total_usage["inputTokens"] += node_result.accumulated_usage.get("inputTokens", 0)
            total_usage["outputTokens"] += node_result.accumulated_usage.get("outputTokens", 0)
            total_usage["totalTokens"] += node_result.accumulated_usage.get("totalTokens", 0)
            total_metrics["latencyMs"] += node_result.accumulated_metrics.get("latencyMs", 0)
            execution_count += node_result.execution_count

        execution_time = self._commit_active_interval(0)
        return MultiAgentResult(
            status=overall_status,
            results=results,
            accumulated_usage=total_usage,
            accumulated_metrics=total_metrics,
            execution_count=execution_count,
            execution_time=execution_time,
        )
