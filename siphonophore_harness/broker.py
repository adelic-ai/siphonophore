"""Broker -- the single call a cognitive loop (or anything else) has for turning an Intent into an
Effect (DESIGN.md section 1). Composes a Gate and an Executor behind one method, dispatch(), so a
caller like CognitiveLoop can hold exactly one capability -- dispatch(intent) -- and nothing else
that could produce a real-world effect.

There is no path from an Intent to an Effect that skips Gate.submit(): dispatch() always mints a
Decision through the Gate first, and Executor.execute() independently re-verifies that Decision's
HMAC token before running anything (mediation.py, execution.py) -- a caller cannot shortcut this by
holding its own Gate/Executor references, because Broker is the only object a CognitiveLoop is
ever given (loop.py).

Kind is never special-cased here: `dispatch()` treats a "delegate" Intent exactly like a
"run_artifact" one -- the same Gate.submit() call, the same Executor.execute() call, the same
execution-class-keyed backend dispatch. Delegation reduces to the same primitive a tool call does
by construction, not by a case Broker adds for it (DESIGN.md section 7).
"""
from __future__ import annotations

from siphonophore_core.execution import Executor
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate


class Broker:
    def __init__(self, gate: Gate, executor: Executor) -> None:
        self._gate = gate
        self._executor = executor

    def dispatch(self, intent: Intent) -> Effect:
        decision = self._gate.submit(intent)
        return self._executor.execute(decision, intent)
