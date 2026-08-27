"""Broker -- the single call a cognitive loop (or anything else) has for turning an Intent into an
Effect (DESIGN.md section 1). Composes a Gate and an Executor behind one method, dispatch(), so a
caller like CognitiveLoop can hold exactly one capability -- dispatch(intent) -- and nothing else
that could produce a real-world effect.

There is no path from an Intent to an Effect that skips Gate.submit(): dispatch() always mints a
Decision through the Gate first, and Executor.execute() independently re-verifies that Decision's
HMAC token before running anything (mediation.py, execution.py) -- a caller cannot shortcut this by
holding its own Gate/Executor references, because Broker is the only object a CognitiveLoop is
ever given (loop.py).

`dispatch()` takes an optional `authority` -- omitted, behavior is unchanged from before this
parameter existed (the authority-less path, `self._gate.submit(intent)`). Given a real, held
`Authority` (authority.py), it's threaded straight through to `Gate.submit(intent,
authority=authority)`, which performs its own independent re-verification of that Authority before
minting anything -- Broker adds no logic of its own here, it only removes the need for a caller
demonstrating delegation to know about `Gate`/`Executor` at all and stitch them together manually.
Granting authority itself (`Gate.issue_order()`/`grant_root_authority()`/`delegate()`) stays outside
Broker entirely -- it's not an Intent, so it was never `dispatch()`'s job. Real multi-agent
orchestration (a second, independently running `CognitiveLoop` actually holding and exercising a
delegated `Authority`) is separate, still-deferred integration work; this only closes the gap where
a single caller demonstrating delegation had to bypass Broker to do it.
"""
from __future__ import annotations

from siphonophore_core.authority import Authority
from siphonophore_core.execution import Executor
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate


class Broker:
    def __init__(self, gate: Gate, executor: Executor) -> None:
        self._gate = gate
        self._executor = executor

    def dispatch(self, intent: Intent, authority: Authority | None = None) -> Effect:
        decision = self._gate.submit(intent, authority=authority)
        return self._executor.execute(decision, intent)
