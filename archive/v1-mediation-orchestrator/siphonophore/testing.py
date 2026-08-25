"""A minimal, deterministic Strands `Model` test double -- for siphonophore's own tests and for
downstream code built on `Colony` to test its own nodes without a real model provider, a network
call, or an API key.

`make_stub_agent` is deliberately a module-level function, not a class method or a closure: a
severed node's recipe (`SeveredRecipe.factory`) must be a "module:qualname" reference the spawned
child process re-imports fresh (see `orchestrator.py`'s docstring for why), so this module doubles
as the simplest possible example of a valid severed-node recipe target, exercised directly by
`tests/test_orchestrator.py` and `tests/test_severed_runner.py`.

Not used by any non-test code in this package.
"""
from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

from strands.agent import Agent
from strands.models.model import Model
from strands.types.event_loop import Metrics, Usage
from strands.types.streaming import StreamEvent


class StubModel(Model):
    """Always answers with the same fixed text, as a single non-streamed content block -- no tool
    calls, no structured output, just enough behavior to drive an `Agent`'s real `stream_async`
    loop to a real, well-formed `AgentResult` without a real model provider on the other end."""

    def __init__(self, text: str = "stub response") -> None:
        self._text = text
        self._config: dict[str, Any] = {}

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> Any:
        return self._config

    async def structured_output(
        self, output_model: Any, prompt: Any, system_prompt: str | None = None, **kwargs: Any
    ) -> Any:
        raise NotImplementedError("StubModel does not support structured output")
        yield  # pragma: no cover -- unreachable; keeps this an async generator per the Model contract

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: str | None = None,
        *,
        tool_choice: Any = None,
        system_prompt_content: Any = None,
        invocation_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": self._text}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": Usage(inputTokens=1, outputTokens=1, totalTokens=2),
                "metrics": Metrics(latencyMs=1),
            }
        }


def make_stub_agent(text: str = "stub response") -> Agent:
    """A module-level, importable factory building an `Agent` backed by `StubModel`. Directly
    usable as a `SeveredRecipe.factory` reference (`"siphonophore.testing:make_stub_agent"`), and
    as a plain live `Agent` for non-severed node tests."""
    return Agent(model=StubModel(text), callback_handler=None)


def make_uid_reporting_agent() -> Agent:
    """A factory whose response text is this process's own real uid -- lets a severed-dispatch
    integration test prove the child process actually ran under the provisioned uid (not the
    broker's own), using nothing beyond the ordinary stdout round trip every severed node already
    goes through."""
    import os

    return Agent(model=StubModel(str(os.getuid())), callback_handler=None)


def make_failing_agent(message: str = "stub failure") -> Agent:
    """A factory whose Agent raises as soon as it's asked to do anything -- for testing that one
    node's failure is reported as that node's own FAILED NodeResult, not a crash that takes the
    whole invocation down with it."""
    return Agent(model=_RaisingModel(message), callback_handler=None)


class _RaisingModel(Model):
    """Raises on the first `stream()` call. Everything else is unused boilerplate to satisfy the
    abstract `Model` contract."""

    def __init__(self, message: str) -> None:
        self._message = message

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> Any:
        return {}

    async def structured_output(
        self, output_model: Any, prompt: Any, system_prompt: str | None = None, **kwargs: Any
    ) -> Any:
        raise NotImplementedError(self._message)
        yield  # pragma: no cover

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterable[StreamEvent]:
        raise RuntimeError(self._message)
        yield  # pragma: no cover -- unreachable; keeps this an async generator
