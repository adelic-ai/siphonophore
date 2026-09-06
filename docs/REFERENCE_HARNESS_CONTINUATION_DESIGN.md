# Reference Harness Operation-Continuation Design

**Status: DESIGN STAGE. No source, test, or example file changes accompany this document.**

Branch `design/reference-harness-operation-continuation`, created from `679e68d` (`core: capture
same-process execution output`), tip of `implement/reference-harness-same-process-output`. This
document designs — but does not implement — the smallest coherent contract for a user turn to
resolve through zero or more mediated operation/result cycles before a final model response, per
`docs/REFERENCE_HARNESS_V1_HORIZON.md`'s Gap 1 ("no iterative turn / model continuation") and its
own named next-design-stage boundary. Every claim is labeled **SOURCE FACT** (re-verified directly
against current code/tests in this stage), **INFERENCE** (a conclusion drawn from source facts,
flagged as such), **DESIGN DECISION** (a choice made here), or **RECOMMENDATION** (a suggestion left
open for the implementation stage or the owner). `~/research` was not accessed.

Source re-verified directly in this stage, in full: `siphonophore_harness/loop.py`, `outcome.py`,
`broker.py`, `intent_parsing.py`, `model.py`, `model_anthropic.py`, `prompts.py`, `composition.py`;
`siphonophore_core/execution.py` (as it now stands after `679e68d`), `mediation.py`, `policy.py`,
`identity.py`, `intent.py`; `examples/repl.py`; `docs/REFERENCE_HARNESS_V1_HORIZON.md` in full.

## Current Problem

**SOURCE FACT**, confirmed directly against `siphonophore_harness/loop.py` as it stands today.
`CognitiveLoop.step()` performs at most one `model.complete()` call, one `parse_turn()` call, and
at most one `Broker.dispatch()` call, then returns unconditionally. Nothing in `step()`,
`broker.py`, or `examples/repl.py`'s `main()` loop ever calls `model.complete()` a second time
within the same user turn using an `Effect`/outcome as input. Today, on any dispatch outcome other
than success, the raised exception (`GateViolation` and its subclasses, `ExecutionError`,
`IdentityError`) propagates out of `step()` uncaught — the REPL's `main()` catches it at the
top level and renders it, but the model itself never sees it and never gets a chance to react.

This means: acceptance scenario C ("inspect this URL and tell me what the project does... model
answers the original question without requiring the user to ask 'What did you find?'") is not met,
and acceptance scenario E ("model/operator receive truthful structured information... user gets an
intelligible final outcome" after a denial) is only half met — the *operator* gets a structured,
truthfully-labeled outcome (`OutcomeCategory`, rendered by `repl.py`), but the *model* never does,
so it cannot produce a conversational explanation of its own denial/failure.

## Verified Current Mechanics

Re-confirmed directly against source in this stage, not accepted from prior documents:

- **`Model` is provider-agnostic** (`model.py:18-26`): `complete(messages: list[dict]) -> str`,
  each message `{"role": ..., "content": ...}`. No Anthropic-specific shape appears anywhere in
  `siphonophore_harness/loop.py`, `broker.py`, or `intent_parsing.py`. This confirms a continuation
  design does not need to invent a new conceptual message shape — it can and should describe every
  interaction in terms of this existing `{"role", "content"}` history contract.
- **History already uses three roles** (`loop.py`): `"user"` (the human's message), `"assistant"`
  (the model's raw completion, verbatim), `"effect"` (a harness-authored, plain-text description of
  what happened). `siphonophore_harness/model_anthropic.py`'s own docstring already discloses that
  it maps `"effect"` to Anthropic's `"user"` role specifically because a delegation's/operation's
  effect is "naturally the next thing 'reported to' the model, the same shape a tool result would
  take" — i.e., **this adapter-level convention already anticipates exactly the continuation
  pattern this document designs**, it is simply never exercised more than zero or one time per turn
  today.
- **`classify_outcome()` (`outcome.py:120-162`) already classifies any `Broker.dispatch()` outcome
  uniformly** — `DispatchResult` on success, or any of `IntentParseError`, `DecisionVerificationError`,
  `ArtifactMismatchError`, `PolicyDeniedError`, `NoBackendRegisteredError`, `IdentityError`,
  `ExecutionError`, or a bare unclassifiable `GateViolation`, on failure — into one of 8 named
  `OutcomeCategory` values, entirely via `isinstance` checks on already-existing types, with no
  message-string parsing. **This function requires zero changes to support continuation** — it is
  already a pure, call-once-per-cycle-compatible classifier; nothing about calling it N times
  instead of at most once changes its contract.
- **Every dispatched `Intent` gets a fresh `intent_id`, and every dispatch mints a fresh `Decision`
  independently** (`intent_parsing.py:128`, `mediation.py:104-122`, `broker.py:53-64`). Nothing in
  the current architecture caches, reuses, or batches a `Decision` or `Intent` across calls — each
  `Broker.dispatch()` call is already, structurally, a complete, independent mediation pass. A
  continuation design that calls `Broker.dispatch()` more than once per turn inherits this for free;
  it does not need to invent a new independence guarantee.
- **`Gate.submit()` mints a `Decision` (with a real, policy-selected `execution_class`) on every
  path except one** (`mediation.py:97-99`, re-read this stage): the sole pre-Decision refusal is a
  forged/mismatched `Authority` (raised inside `Gate.submit()` itself, before `self._policy.evaluate()`
  is even called). Every other outcome — success, ordinary policy DENY, no-backend-registered — has
  a real `Decision` (hence a real `execution_class`) already minted before `Executor.execute()` ever
  raises. `DecisionVerificationError` (forged/tampered Decision) and the still-bare, decision/intent
  correspondence `GateViolation` are, per `outcome.py`'s own docstring and
  `REFERENCE_HARNESS_TURN_BOUNDARY_ASSESSMENT.md` §9 (re-confirmed here), **practically unreachable
  through `Broker.dispatch()`'s own call path**, since `Broker` always executes with the exact
  `Intent` object it just minted the `Decision` from.
- **`intent_id` exists before `Gate.submit()` is ever called** (`intent_parsing.py:128`, uuid4
  minted at parse time) — so even the one pre-Decision refusal case (`authority_rejected`) still has
  a real `intent_id` to attribute, just no `execution_class`.
- **`SameProcessBackend.run()` now bounds captured stdout/stderr at 100,000 characters**
  (`execution.py`, `_MAX_CAPTURED_OUTPUT_CHARS`, landed in `679e68d`, this same session's prior
  stage), truncating with an explicit marker, never silently. `SeparateProcessBackend` still places
  `proc.stdout` into `Effect.detail["stdout"]` **unbounded** — confirmed unchanged by `679e68d`,
  which scoped itself to `same_process` only. Neither backend attaches any output to a raised
  `ExecutionError` — `SameProcessBackend`'s docstring (as amended in `679e68d`) explicitly discloses
  that partial output before a crash is discarded, not attached.
- **The response envelope (`{"message", "operation"}`, `intent_parsing.py`) makes no assumption
  about how many times per turn it is used.** `parse_turn()` takes one completion string and returns
  one `ParsedTurn`; nothing about its schema, its validation, or `parse_intent()`'s narrower
  operation-object parsing refers to "this turn" or counts anything. It is already, structurally, a
  per-completion parser, not a per-turn one.

## Design Invariants

Carried forward from the North Star and the Security/Authority Discipline section, restated
precisely because they govern every choice below:

1. **Every mediated operation is independently, fully mediated** — a fresh `Intent`, a fresh
   `Gate.submit()`, a fresh `Decision`, a fresh `Executor.execute()` call, every single time. No
   cycle within a turn ever reuses, batches, or shortcuts a prior cycle's `Decision`. (Already true
   of the current architecture; this design adds no new mechanism here, it only calls the existing
   one more than once.)
2. **Operation count, authority delegation depth, model reasoning depth, and execution isolation
   strength are four distinct dimensions**, confirmed distinct by source, not merely asserted:
   delegation depth (`Scope.remaining_delegation_depth`, `authority.py`) governs how many times an
   `Authority` may be re-delegated via `Gate.delegate()` — a completely separate mechanism from how
   many times `CognitiveLoop` dispatches within one user turn. Execution isolation strength
   (`execution_class`, chosen per-Decision by `Policy.evaluate()`) is a property of one dispatch, not
   of a turn. This design introduces exactly one new bound — a per-turn operation count — and does
   not let it interact with, widen, or substitute for any of the other three.
3. **The cognitive loop never performs an effect itself.** Every operation this design lets a turn
   request still crosses `Broker.dispatch()` — `CognitiveLoop`'s only capability remains exactly
   what `test_harness_structural_proof.py` already enforces (no effect-producing stdlib import,
   `__init__` accepting nothing beyond `model`, `broker`, `principal_id`, `authority`, plus — per
   this design — one new, inert integer bound; see **Boundedness**).
4. **Conversation that requests no effect never constructs an `Intent`.** Unchanged. A message-only
   completion, at any cycle, ends the turn (or, if it is the *first* completion of the turn, behaves
   exactly as `MessageOnlyResult` does today — a strict subset of the new state machine, not a
   different one).
5. **No core (`siphonophore_core`) file needs to change for this design.** Every mechanism this
   design calls (`Broker.dispatch()`, `classify_outcome()`, `Effect`, `Decision`) already exists and
   already generalizes to repeated, independent invocation. This is checked explicitly in
   **Implementation Boundary** below.
6. **The bound belongs to the harness (session-level), never to core, `ExecutionProfile`,
   `Broker`, or `Gate`.** "How many operations may one user turn request" is a conversational/session
   concept — `ExecutionProfile` composes substrate/policy (`composition.py`), which must remain
   meaningful under a Kubernetes profile exactly as under a portable one, and has no concept of a
   "turn" at all. Putting the bound there (or in `Broker`/`Gate`) would leak a cognitive-loop concern
   into a substrate-neutral primitive — the same layering violation `DESIGN.md` §6/§10 already forbid
   for every other harness-only concept (`message`, `ParsedTurn`, `MessageOnlyResult`).

## Proposed Turn State Machine

**DESIGN DECISION.** States and transitions, one user turn, starting from `CognitiveLoop.step(user_message)`:

```
START
  -> append {"role": "user", "content": user_message} to history
  -> operations := []  (ordered list of OperationOutcome, this turn only)

CYCLE (repeated; see Boundedness for the exit condition on entry):
  MODEL_CALL
    -> completion = model.complete(history)
    -> record last_completion / last_diagnostics (unchanged from today, every cycle, not just the first)
  PARSE
    -> parsed = parse_turn(completion, principal_id)
    -> on IntentParseError:
         -> TERMINATE: propagate IntentParseError (unchanged from today's single-cycle behavior;
            see Success/Denial/Failure Semantics for why this one category does not get a
            continuation chance in V1)
    -> on parsed.intent is None (message-only completion):
         -> append {"role": "assistant", "content": completion} to history
         -> append {"role": "effect", "content": "no operation requested this turn"} to history
            (unchanged wording from today)
         -> TERMINATE: return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=False)
    -> on parsed.intent is not None (operation requested):
         -> if len(operations) >= max_operations_per_turn:
              -> TERMINATE: return TurnResult(message=parsed.message, operations=tuple(operations), exhausted=True)
              -> (the operation is NEVER dispatched -- see Boundedness; whatever the model said
                 alongside this refused request, if anything, is the turn's final message, shown
                 honestly rather than fabricated or discarded)
         -> else: DISPATCH
  DISPATCH  (only reached with budget remaining)
    -> try: dispatch_result = Broker.dispatch(parsed.intent, authority=self._authority)
       except (GateViolation, ExecutionError, IdentityError) as exc: outcome_source = exc
       else: outcome_source = dispatch_result
    -> category = classify_outcome(outcome_source)   # raises ValueError only for the
       decision/intent-correspondence anomaly (see Success/Denial/Failure Semantics)
    -> on category in {INTEGRITY_REJECTED} or classify_outcome raising ValueError:
         -> append {"role": "assistant", "content": completion} to history (diagnostic completeness)
         -> TERMINATE: propagate the original exception (fail closed; see rationale below)
    -> else (EXECUTED, DENIED, AUTHORITY_REJECTED, BACKEND_UNAVAILABLE, IDENTITY_REJECTED, EXECUTION_FAILED):
         -> build one OperationOutcome from outcome_source + category (see Operation Result Representation)
         -> operations.append(outcome)
         -> append {"role": "assistant", "content": completion} to history
         -> append {"role": "effect", "content": <bounded textual description of outcome>} to history
         -> go to MODEL_CALL (next cycle)
```

`TERMINATE` always means: `CognitiveLoop.step()` returns (for `TurnResult`) or raises (for the two
named exception-propagation cases), and no further `model.complete()` call happens for this
`user_message`.

## Operation Result Representation

**DESIGN DECISION.** Two new harness-owned types (exact module placement — `outcome.py` extension
vs. a new module — is an implementation detail, not decided here; `outcome.py` is the natural
choice since it already owns `OutcomeCategory`/`classify_outcome()`).

```
OperationOutcome:
    intent_id: str                    # always present -- minted before Gate.submit() is ever called
    category: OutcomeCategory         # unchanged enum, reused verbatim, not redefined
    execution_class: str | None       # the Decision's execution_class; None only for authority_rejected
                                       # (the one category with no minted Decision at all)
    detail: dict                      # present (non-empty) only for EXECUTED -- the Effect's own
                                       # detail dict, unmodified, already bounded by the backend
                                       # (100,000 chars for same_process; see Boundedness for why
                                       # this is a DIFFERENT bound from what reaches the model)
    reason: str | None                # present only for non-EXECUTED categories -- a short,
                                       # harness-composed, human-readable explanation (see below);
                                       # never a raw exception object, never a traceback

TurnResult:
    message: str | None                        # the turn's final conversational text
    operations: tuple[OperationOutcome, ...]   # every mediated dispatch attempted this turn, in order
    exhausted: bool                            # True only if the operation bound ended the turn
                                                # before a final message was produced
```

**`reason` construction.** **DESIGN DECISION**: derived from the existing exception's own message
text (already hand-authored, secret-free prose in this codebase — confirmed by re-reading every
raise site in `execution.py`/`mediation.py` this stage; none embed a token, digest, or other
sensitive value), capped at a small, fixed character bound (RECOMMENDATION: 2,000 characters — see
**Boundedness** for why this is deliberately much smaller than the 100,000-character output-capture
bound). This is **not** the raw `Decision`, **not** `DecisionProjection`'s `authority_id`/`order_id`,
and **not** the Python exception's type name beyond what `category` already communicates in
plain language — see **Model-Visible vs Operator-Visible Information**.

**Compatibility note, not resolved here (flagged as an open owner decision).** `Broker.dispatch()`
today returns `DispatchResult` (`effect` + `DecisionProjection`) unchanged by this design — nothing
about `Broker`, `outcome.py`'s existing `DispatchResult`, or `execution.py` needs to change.
`OperationOutcome` is a **new, additional, harness-level view** built once per cycle from whatever
`Broker.dispatch()` returned or raised, for the specific purpose of continuation history and final
trace rendering — it does not replace `DispatchResult`. Whether `CognitiveLoop.step()`'s new
`TurnResult` return type should retire `MessageOnlyResult`/expose `DispatchResult` directly for the
single-operation case, or keep them as internal-only types now wrapped by `TurnResult`, is an
implementation-shape decision deferred to the next stage (mirroring how
`REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md` §18 deferred the exact shape of `MessageOnlyResult`
itself, then resolved it trivially at implementation time).

## Model-Visible vs Operator-Visible Information

**DESIGN DECISION**, extending the same "curate for the audience" principle `DecisionProjection`
already established (`broker.py`'s own docstring: excludes `token`/`artifact_digest` because they
are "meaningless outside Gate" / "already implicitly confirmed") one layer further, for a narrower
audience:

| Field | Operator (`--verbose`/trace) | Model (continuation history) |
|---|---|---|
| `intent_id` | Yes (already shown today, `repl.py:154`) | Yes — the model already knows it requested *an* operation; naming it costs nothing and lets a sufficiently capable model correlate across cycles if useful |
| `category` (plain label) | Yes (already shown today) | Yes — required for the model to distinguish "it worked" from "it didn't," per acceptance scenario E |
| `execution_class` | Yes (already shown today) | **RECOMMENDATION: yes, but low-value** — costs nothing to include (it's a short string, not sensitive), and a future, more environment-aware model interaction could use it; not required by any acceptance scenario in this document |
| `authority_id`/`order_id` | Yes, already shown when held (`repl.py:155-156`) | **No** — the model does not choose or reason about authority (it is a session-level configuration the harness supplies to every `Broker.dispatch()` call uniformly, never something the model's `operation` object can name or influence); telling the model about it would expose harness/session internals with no corresponding decision the model can make differently |
| `Decision.token`/`artifact_digest` | Never (already excluded from `DecisionProjection`) | Never |
| `Effect.detail` (captured stdout/stderr) | Yes, full backend-capped content (`--verbose` and the compact trace already print it) | **Yes, but bounded separately and typically more tightly** — see **Boundedness** |
| Raw exception `repr()`/traceback | Never (operator sees `str(exc)` today, not a traceback; `repl.py`'s `render_outcome_error` builds its own text) | Never — `reason` is the same *kind* of curated text the operator already gets, capped independently |
| Raw model completion (this cycle's own JSON) | `--verbose` only (unchanged) | N/A — the model already produced it; it is already in `history` as the `"assistant"` entry for this cycle |

**Rationale, stated once rather than per-row**: the model needs exactly enough to (a) know whether
its request succeeded, (b) know roughly why not when it didn't, and (c) see the content it asked
for when it did — nothing about *how the harness is configured* (authority, session identity,
cryptographic material) is something the model's next completion could meaningfully act on, so none
of it crosses into model-visible text. This is the same principle `DecisionProjection` already
applied to the operator; this design applies a strictly narrower cut of the same information to the
model.

## Success / Denial / Failure Semantics

**DESIGN DECISION**, category by category, reasoned from the source facts above (not from a generic
agent-framework convention):

| Category | Recoverable (fed back, model may explain/continue)? | Rationale |
|---|---|---|
| `EXECUTED` | Yes | The ordinary case; this is the entire point of continuation |
| `DENIED` | Yes | An ordinary, expected, signed policy "no" — exactly acceptance scenario E's own example ("I couldn't fetch the URL because that operation was denied") |
| `BACKEND_UNAVAILABLE` | Yes | An environment/configuration gap (e.g. a `"privileged"` consequence with no `uid_cgroup` backend registered), not a security judgment about the model's request — the model can reasonably explain this or try a lower-consequence approach |
| `IDENTITY_REJECTED` | Yes | An execution-identity/provisioning failure, not a judgment about the request's legitimacy |
| `EXECUTION_FAILED` | Yes | The artifact ran (with real authorization) and failed on its own terms (nonzero exit, an uncaught exception) — squarely the kind of thing a model should be able to explain ("the code raised a ValueError") |
| `AUTHORITY_REJECTED` | Yes | Reached only if the *session's own* held `Authority` fails re-verification — the model never supplies or chooses authority, so this reflects a harness/session-level condition, not something specific to this one request; still safe to surface conversationally (bounded budget prevents any cost from repeated failed attempts against the same broken authority) |
| `INTEGRITY_REJECTED` | **No — terminates the turn** | Signals the `Decision` itself is untrustworthy (forged/tampered) or an artifact-digest mismatch — a genuine trust-boundary anomaly, not an ordinary outcome a model should be allowed to reason around. Also, per **Verified Current Mechanics**, practically unreachable through `Broker.dispatch()`'s own path today — this rule costs nothing in practice and exists for defense in depth if that ever changes |
| Unclassifiable bare `GateViolation` (decision/intent correspondence anomaly; `classify_outcome()` raises `ValueError`) | **No — terminates the turn** | By definition not safe to hand to the model with a truthful label, since no truthful label exists yet; failing closed on "we cannot classify this" is the only defensible choice |
| `INPUT_REJECTED` (`IntentParseError`) | **No — terminates the turn, in V1** | This is not an outcome of a *mediated* operation at all — it is a failure of the model's own completion to describe a well-formed turn, occurring strictly before `Broker.dispatch()` is reachable. See below for why this is deliberately not folded into recoverable continuation yet |

**On `INPUT_REJECTED` specifically — RECOMMENDATION, not a decision made here.** Structurally,
nothing prevents treating a parse failure the same as any other recoverable category (feed back "your
last response was not valid: `<bounded parse-error text>`, please respond again") — this is exactly
`docs/REFERENCE_HARNESS_V1_HORIZON.md`'s Gap 3 ("non-JSON completion has no repair/retry"), and it
would consume the *same* per-turn operation-adjacent budget this design introduces (a malformed
completion should not get unlimited free retries any more than a denied operation gets unlimited
free attempts). This design deliberately does **not** implement that — the task scope for this
stage explicitly excludes "model-output retry/repair" — but the state machine above is shaped so
that a future stage could add it as one more entry in the same `CYCLE` loop (a `PARSE_FAILED,
budget remaining` transition back to `MODEL_CALL` instead of `TERMINATE`) without restructuring
anything else. Recorded here as the natural seam, not implemented.

**On why denial/failure must now be *caught* inside `CognitiveLoop`, not merely propagated —
a material behavior change, called out explicitly.** Today, `GateViolation`/`ExecutionError`/
`IdentityError` propagate out of `CognitiveLoop.step()` uncaught (`examples/repl.py`'s `main()` is
the only catcher). Making these categories recoverable requires `CognitiveLoop` itself to catch them
per-cycle (see the `DISPATCH` state above) so it can construct an `OperationOutcome` and continue the
loop. This is the single largest change to `loop.py`'s existing contract this design proposes — a
caller of `CognitiveLoop.step()` today can assume "an exception means the turn produced no
`TurnResult`"; after this design, that remains true only for `IntentParseError` and the two
anomalous categories above, not for ordinary `DENIED`/`EXECUTION_FAILED`/etc., which become
successful `TurnResult` returns (with `exhausted=False`) carrying a non-`EXECUTED` `OperationOutcome`
inside `operations`. **Every existing test asserting `pytest.raises(GateViolation)` /
`pytest.raises(ExecutionError)` etc. against `CognitiveLoop.step()` (not against `Broker.dispatch()`
directly, which is unaffected) will need updating at implementation time** — flagged here so the
implementation stage does not discover this compatibility break mid-flight.

## History Semantics

**DESIGN DECISION**: no new role, no new message shape. Reuses the existing three-role
`{"role", "content"}` contract (`"user"`, `"assistant"`, `"effect"`) verbatim, repeated once per
cycle instead of at most once per turn. **Confirmed, not merely hoped**: this preserves strict
`assistant`/`user`-equivalent alternation exactly as today, because `model_anthropic.py`'s existing
`"effect"` → Anthropic-`"user"` mapping is applied per history entry, not per turn — N repetitions
of `(assistant, effect)` inside one turn produce the identical alternating shape N=1 already
produces, just longer. No change needed to `model_anthropic.py`.

**Pre-existing wrinkle, not introduced by this design, carried forward unchanged**: today, if
`parse_turn()` raises on the *first* completion of a turn, `completion` is never appended to
`history` at all (only `last_completion`/`last_diagnostics` capture it, for `--verbose` diagnostics).
This already leaves `history` in a state where the next `step()` call's fresh `"user"` entry follows
directly after a *previous* `"user"` entry with nothing in between — a real, already-disclosed
alternation gap (`model_anthropic.py`'s own docstring names this exact risk). This design makes the
same wrinkle reachable at cycle 2..N (a parse failure on a *continuation* completion), not just
cycle 1 — it does not create a new kind of gap, it extends an existing, already-disclosed one to
more call sites. Not addressed by this design; left for whichever future stage addresses Gap 3.

**What each cycle's `"effect"` history entry contains, content-wise (not exact string format,
deferred to implementation)**: for `EXECUTED`, the same information `_describe_effect()` already
composes today (`intent_id`, `execution_class`, `detail`) — unchanged in substance, only the
`detail` value crosses the model-context bound (see **Boundedness**) rather than being shown in
full. For every other recoverable category, an analogous, harness-composed description: category
label, `execution_class` if known, and `reason` (bounded).

## Boundedness

**DESIGN DECISION.**

- **Where the bound lives**: a new `CognitiveLoop.__init__` parameter, e.g.
  `max_operations_per_turn: int` — a session-level configuration value, exactly analogous in kind to
  `principal_id`/`authority` (both already constructor parameters), never a `Broker`/`Gate`/
  `ExecutionProfile` concept (per **Design Invariants** #6). `test_harness_structural_proof.py`'s
  existing enforcement of `CognitiveLoop.__init__`'s accepted parameter set will need one new name
  added to its allow-list at implementation time — a mechanical, expected update, not a weakening of
  what that test protects (it still forbids any *other* new parameter, and this one carries no
  capability of its own, just an integer).
- **What it counts**: the number of `Broker.dispatch()` calls *attempted* this turn, regardless of
  outcome — not just successful executions. **RECOMMENDATION, reasoned explicitly**: a denied or
  failed operation still consumes one full mediation pass and one full model round-trip (a real,
  metered API call under `AnthropicAPIModel`); an adversarial or merely buggy model that keeps
  requesting doomed operations must still be bounded exactly as one that keeps succeeding. Counting
  only successes would let a model retry a denied operation an unlimited number of times within one
  turn, which is precisely the "unbounded autonomous loop" this design must not create.
- **Default value**: **RECOMMENDATION: 4.** Justified against the acceptance scenarios: scenario C
  and D each need exactly 1; scenario F ("model requests another operation after receiving a
  result") needs at least 2; 4 leaves comfortable headroom for a realistic compound request (e.g.
  "fetch this URL, then compute something about what you found") without approaching an open-ended
  loop. Not a load-bearing architectural number — any small, positive, owner-tunable integer
  satisfies this design equally well; 4 is a starting point, not a claim about the one correct value.
- **What happens when the bound is reached**: the operation that would exceed it is **never
  dispatched at all** — not dispatched-then-immediately-denied, not silently dropped. The turn
  terminates with `TurnResult(exhausted=True, message=<whatever the model said alongside the refused
  request, or None>, operations=<everything actually dispatched so far>)`. **DESIGN DECISION,
  considered against an alternative**: this design does *not* give the model one extra "please wrap
  up now" round-trip after the bound is reached. An extra grace round was considered (it could
  improve UX by letting the model always produce a clean final sentence) and rejected for this
  stage: it would require a fourth bound-within-a-bound (how many "please stop" nudges are allowed
  before *that* loop is capped too) and a new history-role concept, for a benefit (a slightly more
  polished sentence on an already-rare exhausted-budget path) that no acceptance scenario in this
  document requires. Recorded as a considered, smaller-scope alternative for a future stage to revisit
  if real usage shows the plain "exhausted" outcome is confusing.
- **Three separate size bounds — do not conflate them (per this design's own explicit instruction
  not to equate storage size with model-context size):**
  1. **Backend/storage capture bound**: `SameProcessBackend`'s existing 100,000-character cap on
     `Effect.detail` (`679e68d`, unchanged by this design). Governs how much a single backend will
     ever place into a single `Effect`, independent of what happens to it afterward.
  2. **Model-context bound (new, this design)**: how much of `Effect.detail`'s content is actually
     included in the `"effect"` history entry text fed to the *next* `model.complete()` call.
     **RECOMMENDATION: a separate, smaller cap — e.g. 4,000 characters** — because every character
     here consumes real API tokens/cost and context-window budget on every subsequent call this
     turn (unlike the storage bound, which is paid once), and the model needs enough to answer a
     question, not full-fidelity forensic detail. Lives in the harness (wherever `OperationOutcome`
     is constructed), never in `siphonophore_core` — core has no concept of "model context."
  3. **UI/display bound**: what the REPL's compact trace line and `--verbose` output print.
     **RECOMMENDATION: reuse the backend-capped value as-is** (already reasonable for a one-off
     terminal print, and `--verbose` is explicitly a developer/operator surface where more detail is
     appropriate, not less) — a genuinely separate concern from (2) because an operator reading a
     terminal is not paying per-character API cost the way a subsequent model call is.
  These three numbers may coincide by coincidence but must never be assumed equal, and a future
  typed operation (e.g. a `fetch_url` backend returning a large page body) makes the distinction
  concrete rather than academic: it could legitimately need a large *storage* capture while still
  needing a much smaller *model-context* slice.

## Presentation Semantics

**DESIGN DECISION.** Answer stays visually primary; a turn's trace becomes the smallest
understandable multi-line extension of today's single line, not a redesign:

```
> inspect this URL and tell me what the project does

<the model's final answer, using what it found>

  [executed] fetch/run_artifact ... intent_id=...
```

For a turn with N > 1 mediated operations (scenario F), the trace becomes N consecutive lines, one
per `OperationOutcome`, in dispatch order, each in the same compact shape `render_turn_result()`
already produces today — no combining, no summarizing, no collapsing multiple operations into one
line:

```
<the model's final answer>

  [executed] run_artifact ... intent_id=...
  [denied] run_artifact ... intent_id=...
```

For an exhausted turn: the same trace lines for every operation actually dispatched, plus one final,
distinctly-worded line that is **not** an `OutcomeCategory` label (it is a harness/session policy
decision, not a `Broker.dispatch()` outcome, and must not be visually confusable with one) —
RECOMMENDATION: `[operation limit reached] N operations attempted this turn`.

## What Happens to the Model's Pre-Operation `message`

**DESIGN DECISION**, reasoned from UX, not from preserving today's accidental single-cycle
behavior. Today, whatever `message` accompanies an operation is always shown, because there is only
ever at most one cycle. With N cycles, showing every intermediate "I'll inspect that now" /
"Now let me check X" line by default would visually compete with the final answer — precisely the
anti-pattern this project already fixed once (Stage 4A's original UX defect: the execution trace
competing with the model's answer, corrected by making the answer print first). Repeating that
mistake across N intermediate messages would be worse, not better.

**Decision**: only the turn's **final** message — the one accompanying the terminal state
(a message-only completion, or the message alongside a refused over-budget request) — is shown as
the primary answer in normal (non-verbose) mode. Every intermediate cycle's own `message` is
demoted to `--verbose`-only material, shown alongside that cycle's raw completion (extending the
existing verbose-only raw-completion convention, not inventing a new one) — **never discarded**,
just not primary. This answers the task's four-option framing directly: "become intermediate/
verbose-only material," chosen over "shown immediately" (would clutter the primary answer) and over
"withheld" (would lose real diagnostic value `--verbose` already exists to provide).

## Interaction With Future Typed Operations

**INFERENCE**, checked directly against this design's own contract, not asserted. Nothing in
`OperationOutcome`, `TurnResult`, the turn state machine, or the history mechanism above reads or
branches on `intent.kind`, `intent.artifact_code`, or any Python-specific concept. The entire
continuation design is built from types and functions that are already generic across `kind`
values: `Intent.intent_id`, `Decision.execution_class`, `classify_outcome()` (already substrate- and
kind-neutral, per its own docstring), and `Effect.detail` (already an opaque, backend-defined dict —
`execution_k8s.py` already populates it with `pod_name`/`namespace`/`node_name`/`phase`/`exit_code`/
`stdout` instead of `same_process`'s `stdout`/`stderr` keys, confirmed as pre-existing heterogeneity
by `REFERENCE_HARNESS_ASSESSMENT.md` §6/§11, not introduced by this design). A future `fetch_url` or
`read_file` operation, backed by a new `ExecutionBackend` producing its own `Effect.detail` shape,
would classify into the same 8 categories via the unmodified `classify_outcome()`, and flow through
the identical `OperationOutcome`/`TurnResult`/history mechanism with **zero changes to this design's
contract**. The only place a new typed operation would ever require a change is `Effect.detail`'s
own per-backend key names — an already-known, already-deferred question (`TARGET_DESIGN.md` §6/§11's
"shared `Effect.detail` shape," still open, not resolved by this document either) — not anything
this continuation design introduces or depends on resolving. This is the strongest evidence this
design sits at the correct layer of abstraction: it never needed to know what an operation *does*,
only whether it was mediated and what category resulted.

## Platform-Independence Check

**INFERENCE.** No Kubernetes (or any other substrate) vocabulary appears anywhere in this design —
`OperationOutcome`, `TurnResult`, `max_operations_per_turn`, and the turn state machine are all
expressed purely in terms of `siphonophore_core` types that already exist above every substrate
boundary (`Intent`, `Decision`, `Effect`, the `OutcomeCategory` enum) plus harness-only conversational
concepts (`message`, history roles) that already exist today. The design is exercised identically
under `portable_profile()`, a hypothetical stronger Linux profile (`uid_cgroup`), or a Kubernetes
profile (`k8s_pod`) — the only thing that varies across profiles is which `execution_class` string
appears in an `OperationOutcome` and what backend-specific keys appear inside `Effect.detail`,
exactly the same variation that already exists today for a single, non-continued dispatch. Nothing
in this design requires `ExecutionProfile`, `compose_profile()`, or any composition helper to change.

## Alternatives Considered

**DESIGN DECISION**, comparison table, evaluated against this design's own requirements:

| Alternative | Verdict |
|---|---|
| **A — Unbounded agentic loop** (model may request operations indefinitely until it stops on its own) | Rejected outright — explicitly forbidden by the task framing ("do not assume an arbitrary agent loop is desirable") and by Design Invariant #3's own spirit; an adversarial or buggy model could otherwise consume unbounded API cost/wall-clock time within one user turn |
| **B — Exactly one continuation round, hardcoded** (model gets a *single* extra completion after *one* operation, never more) | Rejected — closes scenario C but not scenario F ("model requests another operation after receiving a result"), which the task explicitly names as an acceptance scenario this design must explain end to end |
| **C — Bounded N-cycle loop, count = dispatch attempts, harness-level configurable bound** (this design) | **Selected** — satisfies every acceptance scenario (A–G), keeps the bound outside `siphonophore_core`, and reuses every existing mechanism (`classify_outcome()`, history roles, the envelope schema) unchanged |
| **D — Bound expressed as a "reasoning budget" the model itself manages/reports** (the model declares, e.g., "steps_remaining" in its own JSON) | Rejected — would require extending the response envelope with a field the model could misreport or ignore, moving a security-relevant bound into untrusted model output; the current design's bound is enforced entirely by the harness, never trusted from the model, matching the same "never trust the completion" discipline `intent_parsing.py` already applies to `artifact_code`/`kind` |
| **E — Give the model a final "please wrap up" grace round when the bound is reached** | Considered, not selected for this stage — see **Boundedness**'s own discussion; a real, small UX improvement, deferred as unnecessary complexity for V1 |
| **F — Fold `INPUT_REJECTED` into the same recoverable-continuation mechanism now** | Considered, not selected — explicitly out of this stage's scope (task instruction: no model-output retry/repair); the state machine is shaped to make this a small future addition, not a redesign, per **Success/Denial/Failure Semantics** |

## Security / Failure Analysis

**INFERENCE**, checked against the Security/Authority Discipline section's explicit prohibitions:

- **"One authorized operation → arbitrary subsequent effects inside the cognitive loop"**: does not
  occur. Every cycle's `Broker.dispatch()` call is a complete, independent mediation pass (Design
  Invariant #1); nothing about a prior cycle's success or failure changes what the *next* cycle's
  `Gate.submit()`/`Executor.execute()` checks or trusts. A model cannot "chain" authority from one
  operation into a second, more-privileged one — each operation is evaluated against the same
  session-level `Authority` (or lack of one) the loop was constructed with, unchanged by anything
  that happened in an earlier cycle this same turn.
- **Bound cannot be bypassed by the model**: the bound is a plain harness-side integer comparison
  before `Broker.dispatch()` is called, never a field the model's completion can name, set, or
  influence (unlike, say, `consequence`, which the model does declare and which `intent_parsing.py`'s
  own docstring already discloses as "taken as you declare it, with no independent check behind
  it" — the operation-count bound is explicitly not given that same trust-the-model treatment).
- **Anomalous/unclassifiable outcomes fail closed, not open**: `INTEGRITY_REJECTED` and the
  unclassifiable bare `GateViolation` terminate the turn rather than being explained away
  conversationally — chosen specifically because these two conditions indicate the mediation
  boundary itself may be compromised (a forged Decision, a swapped artifact, or a genuinely
  unrecognized condition), and no model-facing text should ever imply that class of failure is
  "just" a denial the user can shrug off.
- **Authority delegation depth is untouched**: nothing in this design reads, checks, or modifies
  `Scope.remaining_delegation_depth`, calls `Gate.delegate()`, or constructs a second `CognitiveLoop`
  — delegation orchestration remains exactly as out-of-scope as every prior stage has kept it.
- **No new secret-bearing information crosses into model-visible text**: verified row-by-row in
  **Model-Visible vs Operator-Visible Information** — `token`, `artifact_digest`, `authority_id`,
  `order_id` are all excluded from `OperationOutcome`, exactly as they are already excluded from the
  operator-facing `DecisionProjection`.

## V1 Acceptance Scenarios

Explained end to end against the state machine above:

**A. "Good Morning."** `parsed.intent is None` on the first cycle → `TERMINATE` immediately with
`TurnResult(message="Good morning to you too.", operations=(), exhausted=False)`. No `Intent`, no
`Broker.dispatch()` call, no execution trace — identical to today's `MessageOnlyResult` path, now
expressed as the zero-operation case of the general state machine.

**B. "What is the capital of Japan?"** Identical to A.

**C. "Inspect this URL and tell me what the project does."** Cycle 1: model requests an operation
(e.g. `run_artifact` fetching the URL); dispatched, `EXECUTED`, `Effect.detail["stdout"]` contains
the fetched content (bounded per **Boundedness**); `OperationOutcome` appended, history updated.
Cycle 2: `model.complete()` is called again with that content in history; the model produces a
message-only completion answering the original question. Final `TurnResult(message=<the answer>,
operations=(one EXECUTED outcome,), exhausted=False)`. Presentation: the answer, then one trace
line — matches the north star's target UX exactly.

**D. "Use Python to calculate..."** Cycle 1: `run_artifact` with real `artifact_code`; `EXECUTED`;
`Effect.detail["stdout"]` carries the computed result. Cycle 2: model explains the result in a
message-only completion. Same shape as C, one operation.

**E. Operation is denied.** Cycle 1: `Broker.dispatch()` raises `PolicyDeniedError`; caught inside
`CognitiveLoop`; `category = DENIED`; `OperationOutcome(category=DENIED, reason="intent ... was not
permitted by policy", execution_class=<whatever the Decision selected>)` appended; history updated
with this bounded, truthful description (never "execution failed," never conflated with
`EXECUTION_FAILED` or `INTEGRITY_REJECTED`). Cycle 2: model produces a message-only completion
("I couldn't fetch the URL because that operation was denied."). Final `TurnResult` carries both the
answer and the one `DENIED` `OperationOutcome` — operator's trace line reads `[denied by policy]`,
unambiguous.

**F. Model requests another operation after receiving a result.** Cycle 1 → `EXECUTED`. Cycle 2:
model's completion contains a *new* `operation` (not message-only). Bound check: if
`len(operations) < max_operations_per_turn`, this is dispatched as a genuinely fresh, independent
`Broker.dispatch()` call (fresh `Intent`, fresh `Decision`) — not a continuation of the first
operation's authorization in any sense. Cycle 3: model finally answers. `TurnResult.operations` has
two entries, both independently mediated, in order.

**G. Operation output is very large.** `SameProcessBackend` already caps storage at 100,000
characters (`679e68d`); this design adds a second, smaller model-context cap (RECOMMENDATION: 4,000
characters) applied when composing the `"effect"` history entry the *next* `model.complete()` call
receives — so a 90,000-character fetched page is stored in full in `Effect.detail` (available to
`--verbose`/the operator), but the model's own next completion is built from a much smaller,
separately-truncated slice, protecting context-window budget and API cost independent of the storage
decision. The REPL's own trace/verbose display reuses the storage-bounded value directly, a third,
independently-reasoned bound. All three limits are named explicitly, distinctly, and are never
assumed equal.

## Implementation Boundary

**SOURCE FACT, re-verified this stage.** Every mechanism this design depends on already exists,
unmodified, in `siphonophore_core`:

- `Broker.dispatch(intent, authority=...)` — unchanged signature, unchanged mediation sequence.
- `classify_outcome()`/`OutcomeCategory` — unchanged, already generic across repeated calls.
- `Intent`/`Decision`/`Effect` — unchanged field shapes.
- `Gate.submit()`/`Executor.execute()` — unchanged control flow, unchanged exception hierarchy.

**No `siphonophore_core` file needs to change for this design to be implemented.** The entire
implementation surface is `siphonophore_harness/` (`loop.py`'s `CognitiveLoop.step()`, a new or
extended `outcome.py`, `test_harness_structural_proof.py`'s allow-list gaining
`max_operations_per_turn`) and `examples/repl.py` (rendering multiple trace lines, an exhausted-turn
label, and demoting intermediate messages to `--verbose`). This matches Design Invariant #5 and
directly confirms the horizon document's own framing that this design stage was worth doing
*before* implementation: the open questions were genuinely about harness-level *shape and policy*
(the bound, recoverability per category, presentation), not about whether core needed to change (it
does not).

## Explicit Non-Goals

Restated and reconfirmed unchanged by this stage, matching the task's own scope boundary:

- Implementing any part of this design (deliberately deferred to the next stage).
- Typed filesystem/fetch/tool operations (`run_artifact` remains the only operation kind exercised;
  see **Interaction With Future Typed Operations** for why this design does not need to anticipate
  their exact shape).
- Filesystem/environment/network confinement or sandboxing improvements to any backend.
- Non-JSON completion retry/repair (Gap 3) — the state machine leaves a seam for it, does not build
  it.
- Authority/delegation orchestration (deciding *when* to delegate, constructing a second
  `CognitiveLoop`).
- Kubernetes-specific behavior of any kind.
- A general REPL redesign beyond the specific rendering changes named in **Presentation Semantics**.
- Resolving `Effect.detail`'s cross-backend shape question (`TARGET_DESIGN.md` §6/§11) — still open,
  not touched here.
- Attaching partial stdout/stderr to a failed artifact's exception (the previous stage's disclosed,
  deferred limitation) — reasoned about explicitly and confirmed **not required** for any of this
  document's V1 acceptance scenarios; remains deferred, not solved incidentally by this design.

## Open Owner Decisions

Genuinely left to the owner, not resolvable from source alone:

1. **The default value of `max_operations_per_turn`.** This document recommends 4 as a starting
   point; any small, positive, tunable integer satisfies the design equally well — the number
   itself is a judgment call about acceptable worst-case cost/latency per turn, not an architectural
   question.
2. **The exact model-context truncation bound** (recommended 4,000 characters) — a cost/UX
   trade-off (smaller saves tokens per continued cycle; larger lets the model reason about more of
   what it fetched), not resolvable from this repository's existing evidence alone.
3. **Whether to give the model a final "please wrap up" grace round when the operation bound is
   reached** (Alternative E) — a real UX trade-off against added complexity, deliberately left
   unresolved for this stage; worth revisiting once real (Mac-trial-style) usage exists to judge
   whether the plain "operation limit reached" outcome reads as confusing in practice.
4. **Whether `CognitiveLoop.step()`'s return-type/exception-propagation break** (denial/failure
   becoming `TurnResult` returns instead of propagated exceptions, for every category except
   `INTEGRITY_REJECTED`/unclassifiable/`INPUT_REJECTED`) is acceptable to land as a single
   compatibility break, or whether the implementation stage should provide any transitional
   affordance for existing callers — this document recommends a clean break (consistent with how
   `61fbdcf` already broke `CognitiveLoop.step()`'s return type once before, precedented, not novel),
   but the owner may prefer otherwise.
5. **Exact module placement for `OperationOutcome`/`TurnResult`** (`outcome.py` extension vs. a new
   module) — a naming/organization preference with no functional consequence, deferred exactly the
   way this project's own prior design documents have deferred equivalent questions.
