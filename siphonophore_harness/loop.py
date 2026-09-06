"""CognitiveLoop -- the minimal native cognitive loop DESIGN.md section 6 requires
siphonophore-harness to own: prompt -> completion -> parse turn -> (zero or more bounded, mediated
operation/result cycles) -> final response
(docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md).

Holds a Model, a Broker, and optionally an Authority it was constructed with -- nothing else.
Never imports os, subprocess, socket, or any other effect-producing stdlib module -- there is no
capability in this file to touch the outside world except `broker.dispatch(intent, authority=...)`,
which always goes through Gate.submit() -> Executor.execute() (broker.py), called independently,
fresh, once per requested operation -- never batched, cached, or reused across cycles. An
`Authority` does not change this: it is an inert value object (authority.py) with no methods of its
own that do anything -- the only thing this class can ever do with it is hand it to
`broker.dispatch()`, which already independently re-verifies it (mediation.py's `Gate.submit()`)
exactly as it would any other authority a caller supplied. Holding one does not add a second path
to an effect; it lets this class exercise authority *given to it*, never authority it derives or
grants itself -- granting (`Gate.issue_order()`/`grant_root_authority()`/`delegate()`) requires a
`Gate` reference, which this class still never holds. `max_operations_per_turn` is likewise inert:
a plain integer this class compares against a running count before ever calling
`broker.dispatch()` -- it is not a capability, and it cannot be widened, read, or influenced by
anything in the model's completion (the bound is a harness-side check, never a field the model's
JSON can name). test_harness_structural_proof.py enforces this by static analysis, not just
convention: it asserts this module, intent_parsing.py, model.py, and broker.py import none of a
blocklist of effect-producing stdlib modules, and that `CognitiveLoop.__init__` accepts nothing
beyond `model`, `broker`, `principal_id`, `authority`, `max_operations_per_turn`.

This is DESIGN.md section 7's proof, made structural rather than merely asserted in prose: the
only object this class holds that can produce an Effect is a Broker, and a Broker's only public
method takes an Intent (and, now, an optional Authority) and always mediates it through the Gate
first. There is no field on Model, ScriptedModel, or the completion text itself through which a
Decision, a Gate secret, or an Executor reference could ever reach this class. Calling
`broker.dispatch()` more than once per turn does not weaken this: each call is a complete,
independent mediation pass, indistinguishable in kind from the single dispatch this class always
performed before continuation existed -- there is still no path from "raw completion text" to
"Effect" that skips Gate.submit() -> Executor.execute(), no matter how many times one turn takes
that path.
"""
from __future__ import annotations

from siphonophore_core.authority import Authority
from siphonophore_core.execution import ExecutionError
from siphonophore_core.identity import IdentityError
from siphonophore_core.mediation import GateViolation

from .broker import Broker
from .intent_parsing import parse_turn
from .model import Model
from .outcome import OperationOutcome, OutcomeCategory, TurnResult, classify_outcome

# V1 default (docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md's Boundedness section): a provisional,
# owner-tunable constant, not an architectural ceiling -- any small, positive integer satisfies the
# design equally well. Counts Broker.dispatch() attempts, regardless of outcome (denied/failed
# attempts still consume one full mediation pass and one real model round-trip, so an adversarial
# or merely buggy model that keeps requesting doomed operations must be bounded exactly as one that
# keeps succeeding).
DEFAULT_MAX_OPERATIONS_PER_TURN = 4

# Separate, deliberately much smaller than SameProcessBackend's 100,000-character backend-capture
# bound (siphonophore_core/execution.py): every character here is re-sent to the model on every
# subsequent cycle this turn, consuming real API tokens/cost and context-window budget, unlike the
# backend capture bound, which is paid once. Applied only when composing the "effect" history text
# fed to the next model.complete() call -- OperationOutcome.detail itself is never mutated or
# re-truncated by this, so operator/--verbose presentation always sees the full, backend-capped
# content.
_MODEL_CONTEXT_DETAIL_CHARS = 4_000

# The two outcome categories that fail closed rather than becoming a recoverable continuation
# input -- see docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md's Success/Denial/Failure Semantics.
# INTEGRITY_REJECTED (a forged/tampered Decision, or an artifact-digest mismatch) signals the
# mediation boundary itself may be compromised, not an ordinary outcome a model should reason
# around; classify_outcome() itself already fails closed for the remaining anomalous case (an
# unclassifiable bare GateViolation) by raising ValueError, which this loop does not catch either.
_FAIL_CLOSED_CATEGORIES = frozenset({OutcomeCategory.INTEGRITY_REJECTED})


class CognitiveLoop:
    def __init__(
        self,
        model: Model,
        broker: Broker,
        principal_id: str,
        authority: Authority | None = None,
        max_operations_per_turn: int = DEFAULT_MAX_OPERATIONS_PER_TURN,
    ) -> None:
        self._model = model
        self._broker = broker
        self._principal_id = principal_id
        self._authority = authority
        self._max_operations_per_turn = max_operations_per_turn
        self.history: list[dict] = []
        self.last_message: str | None = None
        self.last_completion: str | None = None
        self.last_diagnostics: object | None = None

    def step(self, user_message: str) -> TurnResult:
        """One user turn: append the user's message, then run zero or more bounded, independently
        mediated operation/result cycles until the model produces a message-only completion (a
        final answer), the per-turn operation bound is reached, or the completion fails to parse
        (fails closed, unchanged from before continuation existed).

        Each cycle: get a completion, parse it into a ParsedTurn (an optional human-facing message
        and an optional Intent, intent_parsing.parse_turn()). If no operation was requested
        (`parsed.intent is None`), the turn is over -- Broker.dispatch() was never called this
        cycle, and this returns a TurnResult carrying whatever operations preceded it (possibly
        none) and this final message. If an operation was requested but the per-turn bound has
        already been reached, it is REFUSED HERE, before Broker.dispatch() is ever called -- never
        dispatched, never mediated, not even to immediately deny it -- and the turn ends with
        `exhausted=True`, honestly showing whatever the model said alongside that refused request
        (never fabricating a final message it did not produce). Otherwise the Intent is dispatched
        through the Broker exactly as a single-cycle turn always did -- a fresh, independent
        mediation pass, every time -- and classified by the same closed OutcomeCategory set already
        used everywhere else in this codebase. Every category except INTEGRITY_REJECTED and an
        unclassifiable bare GateViolation is recoverable: the outcome is fed back into history as
        this cycle's result, and the loop calls the model again, letting it explain a denial or
        failure conversationally, or request a further operation within the remaining budget. This
        branch is purely mechanical -- "does an operation exist," "is there budget," "which
        category resulted" -- never "should the model be allowed to try again": those judgments are
        exhausted entirely by the fixed rules above, not decided ad hoc per turn.

        `self.last_message` is set from each cycle's parsed completion, overwritten every cycle,
        and reset to None at the start of every step() -- unchanged in spirit from before
        continuation existed, now simply updated once per cycle instead of at most once per turn.
        It is pure display state: nothing about dispatch, the Gate, or the Executor reads it or is
        affected by it.

        `self.last_completion` and `self.last_diagnostics` are set immediately after each cycle's
        `model.complete()` returns -- before `parse_turn()` is even called -- and reset to None at
        the start of every step(), so a caller (the REPL's error-rendering path, under --verbose)
        can still see the most recent cycle's raw completion and model-response diagnostics even
        when `parse_turn()` raises before appending anything to history. `last_diagnostics` is
        whatever `getattr(model, "last_diagnostics", None)` finds -- `Model` itself declares no
        such attribute, so a `Model` implementation that doesn't provide one (e.g. `ScriptedModel`)
        simply leaves this None.

        A malformed or hostile completion (intent_parsing.IntentParseError), at any cycle, still
        propagates rather than being swallowed here -- deciding how to recover from a protocol
        violation by the model's own completion (as opposed to an ordinary mediation outcome) is a
        caller-level policy question this minimal loop does not decide silently, unchanged from
        before continuation existed. INTEGRITY_REJECTED and an unclassifiable bare GateViolation
        propagate for the same reason, deliberately never made recoverable -- see this module's
        `_FAIL_CLOSED_CATEGORIES`."""
        self.last_message = None
        self.last_completion = None
        self.last_diagnostics = None
        self.history.append({"role": "user", "content": user_message})

        operations: list[OperationOutcome] = []
        while True:
            completion = self._model.complete(self.history)
            self.last_completion = completion
            self.last_diagnostics = getattr(self._model, "last_diagnostics", None)
            parsed = parse_turn(completion, self._principal_id)
            self.last_message = parsed.message

            if parsed.intent is None:
                self.history.append({"role": "assistant", "content": completion})
                self.history.append({"role": "effect", "content": "no operation requested this turn"})
                return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=False)

            if len(operations) >= self._max_operations_per_turn:
                return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=True)

            intent = parsed.intent
            try:
                dispatch_result = self._broker.dispatch(intent, authority=self._authority)
            except (GateViolation, ExecutionError, IdentityError) as exc:
                category = classify_outcome(exc)  # may raise ValueError -- unclassifiable, fails closed, uncaught
                if category in _FAIL_CLOSED_CATEGORIES:
                    self.history.append({"role": "assistant", "content": completion})
                    raise
                outcome = _build_operation_outcome(intent.intent_id, exc, category)
            else:
                category = classify_outcome(dispatch_result)
                outcome = _build_operation_outcome(intent.intent_id, dispatch_result, category)

            operations.append(outcome)
            self.history.append({"role": "assistant", "content": completion})
            self.history.append({"role": "effect", "content": _describe_operation_outcome(outcome)})


def _build_operation_outcome(intent_id: str, outcome_source, category: OutcomeCategory) -> OperationOutcome:
    """`outcome_source` is either the `DispatchResult` `Broker.dispatch()` returned (EXECUTED) or
    the exception it raised (every other category) -- `Broker.dispatch()` (broker.py) already
    attaches a `DecisionProjection` to the latter, as `.decision`, whenever a Decision was actually
    minted before the refusal (every category except authority_rejected)."""
    decision = getattr(outcome_source, "decision", None)
    if category is OutcomeCategory.EXECUTED:
        effect = outcome_source.effect
        return OperationOutcome(
            intent_id=intent_id,
            category=category,
            execution_class=effect.execution_class,
            authority_id=decision.authority_id if decision is not None else None,
            order_id=decision.order_id if decision is not None else None,
            detail=effect.detail,
            reason=None,
        )
    return OperationOutcome(
        intent_id=intent_id,
        category=category,
        execution_class=decision.execution_class if decision is not None else None,
        authority_id=decision.authority_id if decision is not None else None,
        order_id=decision.order_id if decision is not None else None,
        detail={},
        reason=str(outcome_source),
    )


def _truncate_for_model_context(text: str) -> str:
    if len(text) <= _MODEL_CONTEXT_DETAIL_CHARS:
        return text
    return (
        text[:_MODEL_CONTEXT_DETAIL_CHARS]
        + f"...[truncated for model context, {len(text)} characters total, {_MODEL_CONTEXT_DETAIL_CHARS} shown]"
    )


def _describe_operation_outcome(outcome: OperationOutcome) -> str:
    """Composes this cycle's `"effect"` history entry -- the text the *next* `model.complete()`
    call actually sees. Deliberately excludes `authority_id`/`order_id` (operator-trace-only, see
    `OperationOutcome`'s own docstring) and applies the model-context bound to whatever `detail`/
    `reason` text it includes -- never the backend-capture bound already applied upstream, and
    never a re-truncation of `outcome.detail` itself (that field is untouched)."""
    if outcome.category is OutcomeCategory.EXECUTED:
        detail_text = _truncate_for_model_context(str(outcome.detail))
        return f"intent {outcome.intent_id} executed via {outcome.execution_class}: {detail_text}"
    execution_class = outcome.execution_class or "n/a"
    reason_text = _truncate_for_model_context(outcome.reason or "")
    return f"intent {outcome.intent_id} {outcome.category.value} via {execution_class}: {reason_text}"
