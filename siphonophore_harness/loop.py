"""CognitiveLoop -- the minimal native cognitive loop DESIGN.md section 6 requires
siphonophore-harness to own: prompt -> completion -> parse turn -> (zero or more bounded, mediated
operation/result cycles, or a compiled WorkOrder) -> final response
(docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md, docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md).

Holds a Model, a Broker, and optionally an Authority it was constructed with -- nothing else with
any capability to touch the outside world. Never imports os, subprocess, socket, or any other
effect-producing stdlib module -- there is no capability in this file to touch the outside world
except `broker.dispatch(intent, authority=...)`, which always goes through
Gate.submit() -> Executor.execute() (broker.py), called independently, fresh, once per requested
operation -- never batched, cached, or reused across cycles. `max_operations_per_turn_safety_net`/
`repeated_operation_limit` are inert integers this class compares against running counts before
ever calling `broker.dispatch()`; neither is a capability, and neither can be widened, read, or
influenced by anything in the model's completion (both bounds are harness-side checks, never a
field the model's JSON can name).

**Logical-turn termination vs. resource/runaway backstops (V1.1 redesign,
docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md).** A logical user turn ends the moment the model
produces a real completion signal -- a message naming no further `"operation"`, or a compiled
`work_order` -- never merely because some number of operations happened. V1's
`max_operations_per_turn=4` conflated a resource guardrail with that completion signal, ending
turns mid-reasoning on ordinary multi-observation questions. Two independently-scoped anomaly
backstops exist instead, neither of which is the primary way a turn ends: a deliberately generous
`max_operations_per_turn_safety_net` (a genuine-runaway ceiling, sized to be reached only by a real
anomaly, never by routine work) and a narrower `repeated_operation_limit` (N consecutive requests
naming the exact same operation is a mechanical retry loop, not "many different reasonable
observations"). `TurnResult.exhausted` means the safety net fired; `TurnResult.loop_detected` means
the repeat detector fired -- distinct in kind, never conflated, because an operator/model
diagnosing "why did this turn end" needs to tell a real anomaly apart from ordinary multi-cycle
completion, which reaches neither field.

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
beyond `model`, `broker`, `principal_id`, `authority`, `max_operations_per_turn_safety_net`,
`repeated_operation_limit`, `max_parse_retries_per_turn`, `event_sink`.

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
merely to keep roles alternating.

**Bounded malformed-output robustness.** A completion that fails `parse_turn()` (non-JSON, or a
schema violation) gets up to `max_parse_retries_per_turn` further chances to self-correct within
THIS SAME turn -- never a new user turn, never a redispatched operation (a parse failure happens
strictly before any Intent exists, so there is nothing to redispatch). This shares the transactional
history model above exactly: the failed completion and a corrective note are appended to the same
staged history a real operation would be, so if every retry is exhausted with nothing else having
happened this turn, the whole sequence (original user message, every failed attempt, every
corrective note) rolls back together; if a real operation already committed earlier in the turn,
these entries land in `self.history` immediately and truthfully, same as any other post-commit
content."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from siphonophore_core.authority import Authority
from siphonophore_core.execution import ExecutionError
from siphonophore_core.identity import IdentityError
from siphonophore_core.mediation import GateViolation

from .broker import Broker
from .intent_parsing import IntentParseError, parse_turn
from .model import Model
from .outcome import OperationOutcome, OutcomeCategory, TurnResult, classify_outcome

# V1.1 (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md's turn-termination redesign): a SAFETY-NET
# ceiling, not a logical-completion signal -- the real completion signal is, and always was,
# `parsed.intent is None`/a compiled `work_order` (below). V1's `DEFAULT_MAX_OPERATIONS_PER_TURN =
# 4` conflated the two: it fired mid-reasoning on an ordinary multi-observation task exactly as
# readily as on a genuine runaway, because 4 was small enough to be routinely reached by legitimate
# work. This constant is deliberately generous -- an order of magnitude above V1's -- so it is
# reached only by a genuine anomaly (a model requesting far more operations than any ordinary
# question needs), never by routine multi-file inspection. Counts Broker.dispatch() ATTEMPTS,
# regardless of outcome (unchanged from V1). Siphonophore's own operations are individually more
# consequential than a typical tool call in a harness with a much larger, much more numerous
# operation vocabulary (research/factory-harness-study/CROSS_HARNESS_SYNTHESIS.md D1) -- while
# still being "large and rarely-hit" in kind, not "a small number that routine work will reach."
DEFAULT_MAX_OPERATIONS_PER_TURN_SAFETY_NET = 50

# A narrower, independently-scoped anomaly detector: N consecutive (kind, consequence, payload,
# artifact_code)-identical operation REQUESTS within one turn (whether or not each one was actually
# dispatched) is treated as a mechanical retry loop, distinct in kind from "the model has many
# different, individually reasonable observations to make" (which this constant must never
# penalize -- reading N different files is not a loop just because it is N operations).
# Deliberately much smaller than the safety net above: a genuine repeated-identical-request loop is
# diagnosable far earlier than "this turn is taking an unreasonable number of operations overall."
DEFAULT_REPEATED_OPERATION_LIMIT = 3

# Separate from the operation bound above -- a malformed completion never reaches Broker.dispatch()
# at all, so it consumes a different resource (model round-trips spent self-correcting, not
# mediation attempts). Small and fixed, matching this stage's own instruction not to build an
# elaborate autonomous repair framework: a model that cannot produce valid JSON after a few tries
# is not going to be rescued by more tries.
DEFAULT_MAX_PARSE_RETRIES_PER_TURN = 2

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
        max_operations_per_turn_safety_net: int = DEFAULT_MAX_OPERATIONS_PER_TURN_SAFETY_NET,
        repeated_operation_limit: int = DEFAULT_REPEATED_OPERATION_LIMIT,
        max_parse_retries_per_turn: int = DEFAULT_MAX_PARSE_RETRIES_PER_TURN,
        event_sink: EventSink | None = None,
    ) -> None:
        self._model = model
        self._broker = broker
        self._principal_id = principal_id
        self._authority = authority
        self._max_operations_per_turn_safety_net = max_operations_per_turn_safety_net
        self._repeated_operation_limit = repeated_operation_limit
        self._max_parse_retries_per_turn = max_parse_retries_per_turn
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
        until the model produces a message-only completion, compiles a WorkOrder, an anomaly
        backstop (the safety net or the repeated-operation detector) fires, or a completion/
        model-transport failure ends the turn.

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
        parse_retries_used = 0
        last_operation_signature: tuple | None = None
        consecutive_repeat_count = 0
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
            except IntentParseError as exc:
                self._emit(
                    "model.failure", turn_id=turn_id, cycle_id=cycle_id,
                    error_type=type(exc).__name__, error_message=str(exc),
                )
                # Bounded robustness policy (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): a
                # malformed completion gets a small, fixed number of chances to self-correct
                # within THIS turn -- never a new user turn, never a redispatched operation (a
                # parse failure happens strictly before any Intent exists, so there is nothing to
                # redispatch). The failed completion and a corrective note are appended to
                # working_history, not to self.history directly, unless a prior real event this
                # turn already forced a commit (see commit()/the module docstring) -- so if every
                # retry is ultimately exhausted with nothing else having happened this turn, the
                # entire speculative sequence (original user message, every failed attempt, every
                # corrective note) rolls back together, exactly as a single immediate failure
                # would have. If a real operation already committed earlier this turn, these
                # entries land in self.history immediately and truthfully, same as any other
                # post-commit content.
                working_history.append({"role": "assistant", "content": completion})
                if parse_retries_used < self._max_parse_retries_per_turn:
                    parse_retries_used += 1
                    working_history.append({
                        "role": "effect",
                        "content": f"your completion did not parse ({exc}); respond again with a single valid JSON envelope",
                    })
                    cycle_index += 1
                    continue
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
                    exhausted=False, loop_detected=False, work_order_id=parsed.work_order.work_order_id,
                )
                return TurnResult(
                    message=parsed.message, operations=tuple(operations), exhausted=False,
                    work_order=parsed.work_order,
                )

            if parsed.intent is None:
                working_history.append({"role": "assistant", "content": completion})
                working_history.append({"role": "effect", "content": "no operation requested this turn"})
                commit()
                self._emit(
                    "turn.completed", turn_id=turn_id, operation_count=len(operations),
                    exhausted=False, loop_detected=False,
                )
                return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=False)

            # Safety-net ceiling (DEFAULT_MAX_OPERATIONS_PER_TURN_SAFETY_NET, module docstring
            # above): a genuine anomaly backstop, never the primary logical-completion signal --
            # that signal is, and always was, `parsed.intent is None`/a compiled `work_order`
            # above. Deliberately generous, so ordinary multi-observation work never reaches it.
            if len(operations) >= self._max_operations_per_turn_safety_net:
                working_history.append({"role": "assistant", "content": completion})
                working_history.append({
                    "role": "effect",
                    "content": (
                        f"operation safety-net limit reached this turn "
                        f"({self._max_operations_per_turn_safety_net} operations) -- this is a "
                        "runaway backstop, not an ordinary per-task budget"
                    ),
                })
                commit()
                self._emit(
                    "turn.completed", turn_id=turn_id, operation_count=len(operations),
                    exhausted=True, loop_detected=False,
                )
                return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=True)

            intent = parsed.intent

            # Repeated-identical-operation detection (DEFAULT_REPEATED_OPERATION_LIMIT, module
            # docstring above): a narrower, independently-scoped anomaly -- N consecutive requests
            # naming the exact same (kind, consequence, payload, artifact_code) is a mechanical
            # retry loop, not "many different reasonable observations" (which must never trip
            # this). Checked, and refused, BEFORE this Nth repeat is ever dispatched -- the first
            # N-1 identical requests are dispatched for real (a model retrying once or twice is
            # not yet a loop); only the Nth consecutive one is refused.
            signature = (intent.kind, intent.consequence, intent.payload, intent.artifact_code)
            if signature == last_operation_signature:
                consecutive_repeat_count += 1
            else:
                consecutive_repeat_count = 1
                last_operation_signature = signature

            if consecutive_repeat_count >= self._repeated_operation_limit:
                working_history.append({"role": "assistant", "content": completion})
                working_history.append({
                    "role": "effect",
                    "content": (
                        f"the same operation (kind={intent.kind!r}) was requested "
                        f"{consecutive_repeat_count} times in a row -- refusing to dispatch it "
                        "again this turn; this looks like a mechanical retry loop, not distinct "
                        "observations, so the turn is ending here"
                    ),
                })
                commit()
                self._emit(
                    "turn.completed", turn_id=turn_id, operation_count=len(operations),
                    exhausted=False, loop_detected=True,
                )
                return TurnResult(
                    message=parsed.message, operations=tuple(operations), exhausted=False,
                    loop_detected=True,
                )

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
        # A failed dispatch may still carry real, backend-captured evidence -- e.g. ExecutionError
        # attaches whatever an artifact printed before it crashed (execution.py). Present only
        # when the exception actually carries one; {} otherwise, unchanged from before.
        detail=getattr(outcome_source, "detail", None) or {},
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
