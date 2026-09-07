"""CognitiveLoop -- the minimal native cognitive loop DESIGN.md section 6 requires
siphonophore-harness to own: prompt -> completion -> parse turn -> (zero or more bounded, mediated
operation/result cycles, or a compiled WorkOrder) -> final response
(docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md, docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md).

Holds a Model, a Broker, and optionally an Authority it was constructed with -- nothing else with
any capability to touch the outside world. Never imports os, subprocess, socket, or any other
effect-producing stdlib module -- there is no capability in this file to touch the outside world
except `broker.dispatch(intent, authority=...)`, which always goes through
Gate.submit() -> Executor.execute() (broker.py), called independently, fresh, once per requested
operation -- never batched, cached, or reused across cycles. `max_operations_per_turn` is an inert
integer this class compares against a running count before ever calling `broker.dispatch()`; it is
not a capability, and it cannot be widened, read, or influenced by anything in the model's
completion (the bound is a harness-side check, never a field the model's JSON can name).

`event_sink`, if given, is likewise inert on its own: a plain callable this class invokes with
already-built, harness-authored dict payloads -- never a path, a file handle, or any object with a
capability of its own. This class never decides HOW an event is durably recorded (that is
session_log.py's job, kept out of this file specifically so that
`test_harness_structural_proof.py`'s "no effect-producing stdlib import" check keeps meaning what
it always has: the only way for CognitiveLoop to produce a real-world effect is through an object
it was explicitly handed, and calling a plain function with a dict is not producing an effect any
more than calling `model.complete()` is). Emitting a session/audit event is control-plane behavior,
not something the model requested -- no event here is ever attributed as an Intent, and this class
never constructs one for its own logging.

`test_harness_structural_proof.py` enforces the effect-producing-import property by static
analysis, not just convention, and further asserts that `CognitiveLoop.__init__` accepts nothing
beyond `model`, `broker`, `principal_id`, `authority`, `max_operations_per_turn`, `event_sink`.

This is DESIGN.md section 7's proof, made structural rather than merely asserted in prose: the
only object this class holds that can produce an Effect is a Broker, and a Broker's only public
method takes an Intent (and an optional Authority) and always mediates it through the Gate first.
Calling `broker.dispatch()` more than once per turn does not weaken this: each call is a complete,
independent mediation pass, indistinguishable in kind from the single dispatch this class always
performed before continuation existed.

**Transactional history (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md's "dangling failed turn"
fix).** `self.history` -- the conversational context handed to `model.complete()` -- is committed
to only at the moment this turn produces something real and truthful: a terminal message (with or
without a WorkOrder), the operation-bound being reached, or a real `Broker.dispatch()` attempt
(successful or not). A turn that fails before any of those (a model-transport exception, or a
completion that fails to parse) with nothing yet committed this turn is rolled back entirely --
including the user's own message -- so it cannot dangle as an unresolved request the NEXT,
unrelated turn's completion gets appended after with no assistant reply in between (the exact
mechanism a real Mac trial reproduced: a parse failure left a bare "user" entry in history, and the
following turn's own "user" entry landed immediately after it with no assistant turn between them,
so the model's next completion addressed both). Once anything commits, it commits permanently for
this turn -- a REAL mediated operation, once it happened, is never later erased merely because a
subsequent cycle in the same turn fails; nothing is ever fabricated (no invented assistant reply)
merely to keep roles alternating."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

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
# design equally well. Counts Broker.dispatch() attempts, regardless of outcome.
DEFAULT_MAX_OPERATIONS_PER_TURN = 4

# Separate, deliberately much smaller than SameProcessBackend's 100,000-character backend-capture
# bound (siphonophore_core/execution.py): every character here is re-sent to the model on every
# subsequent cycle this turn, consuming real API tokens/cost and context-window budget, unlike the
# backend capture bound, which is paid once. Applied only when composing the "effect" history text
# fed to the next model.complete() call -- OperationOutcome.detail itself is never mutated or
# re-truncated by this, so operator/--verbose presentation always sees the full, backend-capped
# content.
_MODEL_CONTEXT_DETAIL_CHARS = 4_000

# Outcome categories that fail closed rather than becoming a recoverable continuation input -- see
# docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md's Success/Denial/Failure Semantics.
# INTEGRITY_REJECTED (a forged/tampered Decision, or an artifact-digest mismatch) signals the
# mediation boundary itself may be compromised, not an ordinary outcome a model should reason
# around. An unclassifiable bare GateViolation (classify_outcome() itself raising ValueError) is
# handled alongside these, not via this set -- see step()'s own try/except around classify_outcome.
_FAIL_CLOSED_CATEGORIES = frozenset({OutcomeCategory.INTEGRITY_REJECTED})

EventSink = Callable[[dict], None]


class CognitiveLoop:
    def __init__(
        self,
        model: Model,
        broker: Broker,
        principal_id: str,
        authority: Authority | None = None,
        max_operations_per_turn: int = DEFAULT_MAX_OPERATIONS_PER_TURN,
        event_sink: EventSink | None = None,
    ) -> None:
        self._model = model
        self._broker = broker
        self._principal_id = principal_id
        self._authority = authority
        self._max_operations_per_turn = max_operations_per_turn
        self._event_sink = event_sink
        self.history: list[dict] = []
        self.last_message: str | None = None
        self.last_completion: str | None = None
        self.last_diagnostics: object | None = None

    def _emit(self, event_type: str, **fields) -> None:
        if self._event_sink is None:
            return
        self._event_sink({
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        })

    def step(self, user_message: str) -> TurnResult:
        """One user turn: zero or more bounded, independently mediated operation/result cycles
        until the model produces a message-only completion, compiles a WorkOrder, the per-turn
        operation bound is reached, or a completion/model-transport failure ends the turn.

        See this module's own docstring for the transactional history model. `self.last_message`/
        `self.last_completion`/`self.last_diagnostics` are reset at the start of every call and
        updated once per cycle, reflecting the most recent cycle regardless of whether it ends up
        committed -- pure display/diagnostic state, read by nothing that affects dispatch."""
        self.last_message = None
        self.last_completion = None
        self.last_diagnostics = None

        turn_id = str(uuid.uuid4())
        self._emit("user.message", turn_id=turn_id, content=user_message)

        working_history = list(self.history)
        working_history.append({"role": "user", "content": user_message})
        committed = False

        def commit() -> None:
            nonlocal working_history, committed
            if not committed:
                self.history[:] = working_history
                working_history = self.history
                committed = True

        operations: list[OperationOutcome] = []
        cycle_index = 0
        while True:
            cycle_id = str(uuid.uuid4())
            if cycle_index > 0:
                self._emit("model.continuation", turn_id=turn_id, cycle_id=cycle_id)
            try:
                completion = self._model.complete(working_history)
            except Exception as exc:
                self._emit(
                    "model.failure", turn_id=turn_id, cycle_id=cycle_id,
                    error_type=type(exc).__name__, error_message=str(exc),
                )
                self._emit("turn.failed", turn_id=turn_id, reason="model_transport_failure")
                raise
            self.last_completion = completion
            self.last_diagnostics = getattr(self._model, "last_diagnostics", None)
            self._emit(
                "model.response", turn_id=turn_id, cycle_id=cycle_id,
                completion=completion, **_diagnostics_fields(self.last_diagnostics),
            )

            try:
                parsed = parse_turn(completion, self._principal_id)
            except Exception as exc:
                self._emit(
                    "model.failure", turn_id=turn_id, cycle_id=cycle_id,
                    error_type=type(exc).__name__, error_message=str(exc),
                )
                self._emit("turn.failed", turn_id=turn_id, reason="input_rejected")
                raise
            self.last_message = parsed.message

            if parsed.work_order is not None:
                working_history.append({"role": "assistant", "content": completion})
                working_history.append({
                    "role": "effect",
                    "content": f"work order compiled (status={parsed.work_order.status}, id={parsed.work_order.work_order_id})",
                })
                commit()
                event_type = "work_order.finalized" if parsed.work_order.is_final else "work_order.draft"
                self._emit(event_type, turn_id=turn_id, cycle_id=cycle_id, work_order=parsed.work_order.to_dict())
                self._emit(
                    "turn.completed", turn_id=turn_id, operation_count=len(operations),
                    exhausted=False, work_order_id=parsed.work_order.work_order_id,
                )
                return TurnResult(
                    message=parsed.message, operations=tuple(operations), exhausted=False,
                    work_order=parsed.work_order,
                )

            if parsed.intent is None:
                working_history.append({"role": "assistant", "content": completion})
                working_history.append({"role": "effect", "content": "no operation requested this turn"})
                commit()
                self._emit("turn.completed", turn_id=turn_id, operation_count=len(operations), exhausted=False)
                return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=False)

            if len(operations) >= self._max_operations_per_turn:
                working_history.append({"role": "assistant", "content": completion})
                working_history.append({"role": "effect", "content": "operation limit reached this turn"})
                commit()
                self._emit("turn.completed", turn_id=turn_id, operation_count=len(operations), exhausted=True)
                return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=True)

            intent = parsed.intent
            self._emit(
                "operation.requested", turn_id=turn_id, cycle_id=cycle_id, intent_id=intent.intent_id,
                kind=intent.kind, consequence=intent.consequence,
            )
            try:
                dispatch_result = self._broker.dispatch(intent, authority=self._authority)
            except (GateViolation, ExecutionError, IdentityError) as exc:
                outcome_source, dispatched_ok = exc, False
            else:
                outcome_source, dispatched_ok = dispatch_result, True

            # A real Broker.dispatch() attempt just happened -- commit whatever accumulated this
            # turn so far, unconditionally, before even classifying the outcome, so a real
            # mediation event is never lost regardless of what happens next.
            working_history.append({"role": "assistant", "content": completion})
            commit()

            try:
                category = classify_outcome(outcome_source)
            except ValueError as exc:
                self._emit(
                    "mediation.decision", turn_id=turn_id, cycle_id=cycle_id, intent_id=intent.intent_id,
                    category="unclassifiable",
                )
                self._emit("turn.failed", turn_id=turn_id, reason="unclassifiable_outcome", error_message=str(exc))
                raise

            decision = getattr(outcome_source, "decision", None)
            self._emit(
                "mediation.decision", turn_id=turn_id, cycle_id=cycle_id, intent_id=intent.intent_id,
                category=category.value,
                execution_class=decision.execution_class if decision is not None else None,
                authority_id=decision.authority_id if decision is not None else None,
                order_id=decision.order_id if decision is not None else None,
            )

            if not dispatched_ok and category in _FAIL_CLOSED_CATEGORIES:
                self._emit(
                    "operation.result", turn_id=turn_id, cycle_id=cycle_id, intent_id=intent.intent_id,
                    category=category.value, reason=str(outcome_source),
                )
                self._emit("turn.failed", turn_id=turn_id, reason=category.value)
                raise outcome_source

            outcome = _build_operation_outcome(intent.intent_id, outcome_source, category)
            self._emit(
                "operation.result", turn_id=turn_id, cycle_id=cycle_id, intent_id=intent.intent_id,
                category=category.value, detail=outcome.detail, reason=outcome.reason,
            )
            operations.append(outcome)
            working_history.append({"role": "effect", "content": _describe_operation_outcome(outcome)})
            cycle_index += 1


def _diagnostics_fields(diagnostics: object) -> dict:
    """Extracts only the already-safe, already-structural fields `ModelResponseDiagnostics`
    exposes (model_anthropic.py) -- never raw SDK response objects, never hidden reasoning
    content. Returns {} for a Model that provides no diagnostics (e.g. ScriptedModel), so the
    event simply omits these keys rather than logging fabricated Nones."""
    if diagnostics is None:
        return {}
    return {
        "block_types": list(getattr(diagnostics, "block_types", ())),
        "text_block_count": getattr(diagnostics, "text_block_count", None),
        "retained_text_length": getattr(diagnostics, "retained_text_length", None),
        "stop_reason": getattr(diagnostics, "stop_reason", None),
    }


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
