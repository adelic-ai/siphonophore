"""Model -- the cognitive loop's only source of new intent (DESIGN.md section 6: "a minimal
native cognitive loop"). Deliberately minimal and provider-agnostic: this module owns no HTTP
client, no provider SDK, nothing that reaches outside the process. A real network-backed Model
implementation is future, deliberately deferred work -- wiring one in is an integration decision,
not a cognitive-loop design decision, and adding one now without a specific need would just be a
dependency for its own sake (DESIGN.md section 0).

ScriptedModel is the reference implementation used to prove the vertical slice (DESIGN.md section
7): a fixed, ordered sequence of completions, one per call, so a test can construct an exact
scenario -- including a hostile completion attempting to describe more authority than was granted
-- without needing a real model in the loop at all.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Model(ABC):
    """Turns a conversation (a list of {"role": ..., "content": ...} messages) into one completion
    string. The cognitive loop never inspects HOW a Model produces its completion -- only that it
    returns text, which is then parsed as an Intent (intent_parsing.py). A Model has no access to
    Gate, Executor, or any Effect-producing object; it can only return text."""

    @abstractmethod
    def complete(self, messages: list[dict]) -> str:
        ...


class ScriptedModel(Model):
    """Returns a fixed sequence of completions, one per call, in order. Raises once exhausted -- a
    scripted scenario that calls complete() more times than it provisioned for is a bug in the
    test/demo driving it, not something to silently paper over with a default response."""

    def __init__(self, completions: list[str]) -> None:
        self._completions = list(completions)
        self._index = 0

    def complete(self, messages: list[dict]) -> str:
        if self._index >= len(self._completions):
            raise RuntimeError(f"ScriptedModel exhausted after {self._index} completions")
        completion = self._completions[self._index]
        self._index += 1
        return completion
