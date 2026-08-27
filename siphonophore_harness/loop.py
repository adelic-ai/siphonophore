"""CognitiveLoop -- the minimal native cognitive loop DESIGN.md section 6 requires
siphonophore-harness to own: prompt -> completion -> parse intent -> feed result back.

Holds a Model, a Broker, and optionally an Authority it was constructed with -- nothing else.
Never imports os, subprocess, socket, or any other effect-producing stdlib module -- there is no
capability in this file to touch the outside world except `broker.dispatch(intent, authority=...)`,
which always goes through Gate.submit() -> Executor.execute() (broker.py). An `Authority` does not
change this: it is an inert value object (authority.py) with no methods of its own that do
anything -- the only thing this class can ever do with it is hand it to `broker.dispatch()`, which
already independently re-verifies it (mediation.py's `Gate.submit()`) exactly as it would any other
authority a caller supplied. Holding one does not add a second path to an effect; it lets this
class exercise authority *given to it*, never authority it derives or grants itself -- granting
(`Gate.issue_order()`/`grant_root_authority()`/`delegate()`) requires a `Gate` reference, which this
class still never holds. test_harness_structural_proof.py enforces this by static analysis, not
just convention: it asserts this module, intent_parsing.py, model.py, and broker.py import none of
a blocklist of effect-producing stdlib modules, and that `CognitiveLoop.__init__` accepts nothing
beyond `model`, `broker`, `principal_id`, `authority`.

This is DESIGN.md section 7's proof, made structural rather than merely asserted in prose: the
only object this class holds that can produce an Effect is a Broker, and a Broker's only public
method takes an Intent (and, now, an optional Authority) and always mediates it through the Gate
first. There is no field on Model, ScriptedModel, or the completion text itself through which a
Decision, a Gate secret, or an Executor reference could ever reach this class.
"""
from __future__ import annotations

from siphonophore_core.authority import Authority
from siphonophore_core.intent import Effect

from .broker import Broker
from .intent_parsing import parse_intent
from .model import Model


class CognitiveLoop:
    def __init__(self, model: Model, broker: Broker, principal_id: str, authority: Authority | None = None) -> None:
        self._model = model
        self._broker = broker
        self._principal_id = principal_id
        self._authority = authority
        self.history: list[dict] = []
        self.last_message: str | None = None

    def step(self, user_message: str) -> Effect:
        """One turn: append the user's message, get a completion, parse it into an Intent (plus
        an optional human-facing message, intent_parsing.ParsedTurn), dispatch the Intent through
        the Broker, and feed the resulting Effect back into history as the next turn's context --
        so the model sees what actually happened, not merely what its own prior completion claimed
        it would do.

        `self.last_message` is set from the parsed completion before dispatch is attempted, and
        reset to None at the start of every step() -- so it reflects this turn's own message (or
        the honest absence of one) even if dispatch is refused, and never carries stale text over
        from a previous turn if this one fails to parse at all. It is pure display state: nothing
        about dispatch, the Gate, or the Executor reads it or is affected by it.

        A malformed or hostile completion (intent_parsing.IntentParseError) or a denied/refused
        dispatch (GateViolation and subclasses, from broker.py) propagates rather than being
        swallowed here -- deciding how to recover from either is a caller-level policy question
        (retry, apologize to the user, escalate), not something this minimal loop should decide
        silently on the caller's behalf."""
        self.last_message = None
        self.history.append({"role": "user", "content": user_message})
        completion = self._model.complete(self.history)
        parsed = parse_intent(completion, self._principal_id)
        self.last_message = parsed.message
        effect = self._broker.dispatch(parsed.intent, authority=self._authority)
        self.history.append({"role": "assistant", "content": completion})
        self.history.append({"role": "effect", "content": _describe_effect(effect)})
        return effect


def _describe_effect(effect: Effect) -> str:
    return f"intent {effect.intent_id} executed via {effect.execution_class}: {effect.detail}"
