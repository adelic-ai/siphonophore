"""Tests for `Colony`, the `MultiAgentBase` orchestrator.

Non-severed dispatch, node registration, recipe resolution, and the check-in timeout logic are all
plain Python / real-but-unprivileged Unix sockets -- they run everywhere and are tested for real
here, no mocking of anything root/cgroup/kernel-boundary-related.

Full severed-node dispatch (`identity.provision_identity`, real uid switch via `user=`,
`add_pid_to_cgroup`) needs real root on real Linux -- written and wired end to end in
`orchestrator.py`, exercised here only by `linux_root_only`-marked tests, correctly skipped
everywhere else, same discipline as `test_identity.py`/`test_checkin.py`.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import types

import pytest
from strands.multiagent.base import Status

from siphonophore import checkin, identity
from siphonophore.orchestrator import (
    Colony,
    CheckinTimeoutError,
    DuplicateNodeError,
    OrchestratorError,
    RecipeError,
    SeveredRecipe,
    UnknownNodeError,
    build_argv,
    parse_runner_output,
    resolve_factory,
)
from siphonophore.testing import make_failing_agent, make_stub_agent

linux_root_only = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="provisions real OS users and real cgroups, needs real root on real Linux",
)


def run(coro):
    return asyncio.run(coro)


# --- non-severed dispatch -----------------------------------------------------------------------


def test_single_nonsevered_node_completes():
    colony = Colony()
    colony.add_node("a", make_stub_agent("hello a"))
    result = run(colony.invoke_async("hi"))

    assert result.status == Status.COMPLETED
    assert set(result.results) == {"a"}
    assert result.results["a"].status == Status.COMPLETED
    assert str(result.results["a"]).strip() == "hello a"
    assert result.accumulated_usage["totalTokens"] == 2
    assert result.execution_count == 1


def test_multiple_nonsevered_nodes_dispatch_concurrently_and_both_complete():
    colony = Colony()
    colony.add_node("a", make_stub_agent("hello a"))
    colony.add_node("b", make_stub_agent("hello b"))
    result = run(colony.invoke_async("hi"))

    assert result.status == Status.COMPLETED
    assert set(result.results) == {"a", "b"}
    assert str(result.results["a"]).strip() == "hello a"
    assert str(result.results["b"]).strip() == "hello b"
    assert result.accumulated_usage["totalTokens"] == 4  # 2 nodes x 2 tokens each
    assert result.execution_count == 2


def test_one_failing_node_is_reported_as_that_nodes_own_failure_not_a_crash():
    colony = Colony()
    colony.add_node("ok", make_stub_agent("fine"))
    colony.add_node("bad", make_failing_agent("boom"))
    result = run(colony.invoke_async("hi"))

    assert result.status == Status.FAILED  # overall reflects the failure
    assert result.results["ok"].status == Status.COMPLETED  # but the healthy node still completed
    assert str(result.results["ok"]).strip() == "fine"
    assert result.results["bad"].status == Status.FAILED
    assert isinstance(result.results["bad"].result, Exception)
    assert "boom" in str(result.results["bad"].result)


def test_invoke_async_can_select_a_subset_of_registered_nodes():
    colony = Colony()
    colony.add_node("a", make_stub_agent("a"))
    colony.add_node("b", make_stub_agent("b"))
    result = run(colony.invoke_async("hi", invocation_state={"nodes": ["b"]}))
    assert set(result.results) == {"b"}


def test_invoke_async_rejects_an_unknown_requested_node():
    colony = Colony()
    colony.add_node("a", make_stub_agent("a"))
    with pytest.raises(UnknownNodeError):
        run(colony.invoke_async("hi", invocation_state={"nodes": ["nope"]}))


def test_invoke_async_rejects_when_no_nodes_are_registered_at_all():
    colony = Colony()
    with pytest.raises(OrchestratorError):
        run(colony.invoke_async("hi"))


def test_add_node_rejects_a_duplicate_node_id():
    colony = Colony()
    colony.add_node("a", make_stub_agent("a"))
    with pytest.raises(DuplicateNodeError):
        colony.add_node("a", make_stub_agent("a-again"))


def test_severed_and_nonsevered_node_ids_share_one_namespace():
    colony = Colony()
    colony.add_node("a", make_stub_agent("a"))
    with pytest.raises(DuplicateNodeError):
        colony.add_severed_node("a", SeveredRecipe(factory="siphonophore.testing:make_stub_agent"))


# --- severed-node registration / recipe resolution (no root needed: import-only checks) ----------


def test_add_severed_node_accepts_a_valid_module_level_factory():
    colony = Colony()
    colony.add_severed_node("x", SeveredRecipe(factory="siphonophore.testing:make_stub_agent"))
    assert "x" in colony.node_ids


def test_add_severed_node_rejects_an_unimportable_module():
    colony = Colony()
    with pytest.raises(RecipeError):
        colony.add_severed_node("x", SeveredRecipe(factory="not_a_real_module_xyz:fn"))


def test_add_severed_node_rejects_a_missing_qualname():
    colony = Colony()
    with pytest.raises(RecipeError):
        colony.add_severed_node("x", SeveredRecipe(factory="siphonophore.testing:does_not_exist"))


def test_add_severed_node_rejects_a_reference_missing_the_colon():
    colony = Colony()
    with pytest.raises(RecipeError):
        colony.add_severed_node("x", SeveredRecipe(factory="siphonophore.testing.make_stub_agent"))


def test_resolve_factory_rejects_a_closure_because_a_fresh_import_cannot_see_its_scope():
    """The actual regression this pins: a severed node's child process re-imports the module fresh
    -- it has no access to a closure's captured enclosing scope, so a factory that resolves to a
    closure/local function must be rejected at registration time, not discovered as a mysterious
    failure inside an already-spawned child."""

    def outer():
        def inner():
            pass  # pragma: no cover -- never called; the point is its __qualname__

        return inner

    mod = types.ModuleType("siphonophore_test_fake_module")
    mod.closure_fn = outer()
    sys.modules[mod.__name__] = mod
    try:
        with pytest.raises(RecipeError, match="closure/local function"):
            resolve_factory(f"{mod.__name__}:closure_fn")
    finally:
        del sys.modules[mod.__name__]


def test_resolve_factory_rejects_a_non_callable():
    mod = types.ModuleType("siphonophore_test_fake_module_2")
    mod.not_callable = 42
    sys.modules[mod.__name__] = mod
    try:
        with pytest.raises(RecipeError):
            resolve_factory(f"{mod.__name__}:not_callable")
    finally:
        del sys.modules[mod.__name__]


# --- check-in timeout enforcement (real CheckinServer, no root, no Linux-only feature needed) ----


def test_await_checkin_times_out_for_real_when_nothing_ever_checks_in():
    colony = Colony()
    socket_path = os.path.join(tempfile.mkdtemp(), "colony-test.sock")
    colony._checkin_server = checkin.CheckinServer(socket_path)
    colony._checkin_server.start()
    try:
        start = time.monotonic()
        verified = run(colony._await_checkin("nobody-checked-in", timeout=0.2))
        elapsed = time.monotonic() - start
        assert verified is False
        assert elapsed >= 0.2  # a real deadline was honored, not a fabricated instant return
    finally:
        colony._checkin_server.stop()


def test_await_checkin_returns_true_promptly_once_verified():
    colony = Colony()
    socket_path = os.path.join(tempfile.mkdtemp(), "colony-test.sock")
    colony._checkin_server = checkin.CheckinServer(socket_path)
    colony._checkin_server.start()
    try:
        colony._checkin_server._verified["already-verified"] = object()  # simulate a prior check-in
        verified = run(colony._await_checkin("already-verified", timeout=5.0))
        assert verified is True
    finally:
        colony._checkin_server.stop()


# --- severed-node argv / result-envelope plumbing (pure functions, no process spawn) -------------


def test_build_argv_shape():
    recipe = SeveredRecipe(factory="siphonophore.testing:make_stub_agent", kwargs={"text": "hi"})
    argv = build_argv("/tmp/whatever.sock", "the-nonce", recipe, "do the task", {"k": 1})

    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "siphonophore._severed_runner"]
    assert argv[3] == "/tmp/whatever.sock"
    assert argv[4] == "the-nonce"

    import json

    payload = json.loads(argv[5])
    assert payload == {
        "factory": "siphonophore.testing:make_stub_agent",
        "kwargs": {"text": "hi"},
        "task": "do the task",
        "invocation_state": {"k": 1},
    }


def test_parse_runner_output_round_trips_a_real_agent_result():
    """Builds a real AgentResult the same way the severed runner would, serializes it through the
    same envelope shape `_severed_runner._build_envelope` produces, and confirms the orchestrator
    side reconstructs it -- including the usage/metrics/interrupts that AgentResult.to_dict()
    itself deliberately drops (see parse_runner_output's own docstring)."""
    import json

    from siphonophore._severed_runner import _build_envelope

    result = run(_agent_result_from_stub("real result text"))
    envelope = _build_envelope(result)
    stdout = (json.dumps(envelope) + "\n").encode()

    agent_result, usage, metrics, interrupts = parse_runner_output("some-node", stdout)
    assert str(agent_result).strip() == "real result text"
    assert usage["totalTokens"] == 2
    assert metrics["latencyMs"] == 1
    assert interrupts == []


def test_parse_runner_output_raises_on_empty_stdout():
    with pytest.raises(OrchestratorError):
        parse_runner_output("some-node", b"")


def test_parse_runner_output_raises_on_garbage():
    with pytest.raises(OrchestratorError):
        parse_runner_output("some-node", b"not json at all")


async def _agent_result_from_stub(text: str):
    agent = make_stub_agent(text)
    result = None
    async for event in agent.stream_async("hi"):
        if "result" in event:
            result = event["result"]
    assert result is not None
    return result


@pytest.mark.skipif(
    sys.platform == "linux" and os.geteuid() == 0,
    reason="this host can really provision identities; the without-privilege failure path isn't exercised here",
)
def test_severed_dispatch_without_real_privilege_fails_cleanly_as_that_nodes_own_failure():
    """Runs everywhere else, including right here without root: attempting to actually provision
    an identity (`identity.provision_identity`, which needs real `useradd`/cgroupfs) without the
    privilege to do so must raise for real -- never silently succeed, never fabricate an identity.
    That real failure must land as this one node's own FAILED NodeResult, not a crash that takes
    the whole invocation down, and never something disguised as a skip. This is the actual proof
    that the severed path refuses to fake what it cannot really provision."""
    colony = Colony()
    colony.add_severed_node("x", SeveredRecipe(factory="siphonophore.testing:make_stub_agent"))
    result = run(colony.invoke_async("hi"))

    assert result.status == Status.FAILED
    assert result.results["x"].status == Status.FAILED
    assert isinstance(result.results["x"].result, Exception)


# --- full severed dispatch: real root, real Linux only --------------------------------------------


@linux_root_only
def test_severed_node_dispatch_runs_under_the_provisioned_uid_end_to_end():
    """The actual proof of the whole pipeline: a severed node's response is its own os.getuid(),
    which must land in the reserved node range and must NOT equal this test process's own uid --
    proving the child really ran as the provisioned identity, not the broker's."""
    colony = Colony()
    colony.add_severed_node("uid-reporter", SeveredRecipe(factory="siphonophore.testing:make_uid_reporting_agent"))

    result = run(colony.invoke_async("hi"))

    assert result.status == Status.COMPLETED
    node_result = result.results["uid-reporter"]
    assert node_result.status == Status.COMPLETED
    reported_uid = int(str(node_result).strip())
    assert reported_uid != os.getuid()
    assert identity.NODE_UID_MIN <= reported_uid <= identity.NODE_UID_MAX


@linux_root_only
def test_severed_node_that_never_checks_in_times_out_and_is_reported_failed():
    """A recipe whose factory hangs before check-in would need real process control to simulate
    honestly; instead this proves the timeout path using a real, absurdly short timeout against a
    real (slower) severed spawn -- if the child ever does check in within the window this test's
    assumption is wrong and it should be revisited, not loosened into a mock."""
    colony = Colony()
    colony.add_severed_node(
        "too-slow",
        SeveredRecipe(factory="siphonophore.testing:make_stub_agent"),
        checkin_timeout=0.0001,
    )
    result = run(colony.invoke_async("hi"))
    assert result.status == Status.FAILED
    assert isinstance(result.results["too-slow"].result, CheckinTimeoutError)
