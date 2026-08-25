"""Entrypoint for a severed node's spawned child process.

Invoked by `Colony._dispatch_severed` as:

    python -m siphonophore._severed_runner <checkin_socket_path> <nonce_fd> <payload_json>

where `payload_json` decodes to `{"factory": "module:qualname", "kwargs": {...}, "task": ...,
"invocation_state": {...}}` -- see `orchestrator.build_argv`. The socket path, recipe reference,
and task travel via argv (none of them are secrets). The check-in nonce deliberately does not:
`nonce_fd` is a file descriptor number, inherited from the parent via `pass_fds` at spawn time,
whose *content* (read exactly once, here) is the actual nonce. Checked directly against a real
Linux host, not assumed: `/proc/<pid>/cmdline` is world-readable (mode 0444) for this process's
entire lifetime, so a nonce placed in argv would be readable by any local process, any uid, the
whole time this process runs -- not just briefly. A pipe fd has no such exposure; nothing besides
the two ends of that specific pipe can read it, regardless of uid.

The check-in is this process's first action, before touching its own recipe or task at all -- a
node that did real work before checking in would have already defeated the point: the check-in is
what makes anything this process does afterward attributable with confidence, not a formality
that can happen whenever.

Result delivery: this process's stdout, captured by the parent via a pipe (the "pipe... mechanism"
the build prompt names as one of the two sanctioned options) -- a single line of JSON on success.
See `orchestrator.parse_runner_output` for why the payload is a small envelope around
`AgentResult.to_dict()`, not that dict alone.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from strands.agent.agent_result import AgentResult

from . import checkin
from .orchestrator import resolve_factory


class SeveredRunnerError(RuntimeError):
    """This process itself failed before or during producing a result -- distinct from the
    delegated Agent raising, which is instead captured and re-raised by the caller's own
    exception, propagating normally as this process's failure too (there is no "half of a severed
    node's own run failing" -- an uncaught exception here is exactly this process's exit code
    reporting failure, same as any other program)."""


async def _run_agent(factory_ref: str, kwargs: dict[str, Any], task: Any, invocation_state: dict[str, Any]) -> AgentResult:
    factory = resolve_factory(factory_ref)
    agent = factory(**kwargs)
    result: AgentResult | None = None
    async for event in agent.stream_async(task, invocation_state=invocation_state):
        if "result" in event:
            result = event["result"]
    if result is None:
        raise SeveredRunnerError("severed node's agent did not produce a result event")
    return result


def _build_envelope(result: AgentResult) -> dict[str, Any]:
    """Pairs `AgentResult.to_dict()` with the usage/metrics/interrupts it doesn't itself
    serialize (checked against the installed package -- see `orchestrator.parse_runner_output`),
    so the parent's reconstructed NodeResult carries this node's real numbers, not zeros."""
    metrics = getattr(result, "metrics", None)
    usage = dict(getattr(metrics, "accumulated_usage", {}) or {})
    perf_metrics = dict(getattr(metrics, "accumulated_metrics", {}) or {})
    return {
        "agent_result": result.to_dict(),
        "usage": usage,
        "metrics": perf_metrics,
        "interrupts": [i.to_dict() for i in (result.interrupts or [])],
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print(
            "usage: python -m siphonophore._severed_runner <socket_path> <nonce_fd> <payload_json>",
            file=sys.stderr,
        )
        return 2
    socket_path, nonce_fd_str, payload_json = args
    payload = json.loads(payload_json)

    # Read the nonce exactly once, from the inherited pipe fd -- not argv (see module docstring
    # for why: /proc/<pid>/cmdline is world-readable, this pipe fd is not readable by anything
    # outside its own two ends). The fd is closed by this read; there is nothing else on it.
    nonce_fd = int(nonce_fd_str)
    with os.fdopen(nonce_fd, "r") as f:
        nonce = f.read()

    # First action, full stop: check in, before touching the recipe or task at all.
    checkin.check_in(socket_path, nonce)

    result = asyncio.run(
        _run_agent(
            payload["factory"],
            payload.get("kwargs") or {},
            payload["task"],
            payload.get("invocation_state") or {},
        )
    )
    envelope = _build_envelope(result)
    sys.stdout.write(json.dumps(envelope))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
