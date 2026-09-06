# Reference Harness Turn Contract — Design

Design stage. Branch `design/reference-harness-turn-contract`, created from
`70a45e9be6f503bdc86de4516f163ce0d220ed48` (tip of
`investigate/reference-harness-turn-boundary`,
`docs/REFERENCE_HARNESS_TURN_BOUNDARY_ASSESSMENT.md`). This document selects and specifies a
target model-response / requested-operation contract for the reference harness. **No source or
test file is changed by this stage.** Every claim below is labeled **CURRENT FACT** (re-verified
directly against source in this stage, not merely cited from the assessment), **DESIGN DECISION**
(a choice made here), **INFERENCE** (a conclusion drawn from CURRENT FACT, flagged as such), or
**OPEN QUESTION** (left for the next stage or the project to resolve).

Source re-verified directly in this stage (full file reads, not excerpt trust):
`siphonophore_harness/intent_parsing.py`, `siphonophore_harness/prompts.py`,
`siphonophore_harness/loop.py`, `siphonophore_harness/broker.py`, `siphonophore_core/execution.py`,
plus `siphonophore_harness/outcome.py`, `siphonophore_core/intent.py`,
`siphonophore_harness/composition.py`, and `examples/repl.py` (needed to verify the assessment's
own citations of `DispatchResult`/`OutcomeCategory`/`render_turn_result`/`ExecutionProfile`). Every
line-numbered claim in the assessment's §3, §4, §6, §7, §8, §9, §12, §13, §16 was independently
confirmed against the current tree; none were found stale or misquoted.

---

## 1. Decision

**DESIGN DECISION**: **Option C — Response Envelope With Optional Action.**

The model-facing contract becomes a single JSON envelope with two independently optional
top-level keys: `"message"` (human-facing text, unchanged in meaning from today) and
`"operation"` (a requested effect, structurally identical in spirit to today's flat intent
fields, but now nested and, when present, required to carry `artifact_code`).

```json
{"message": "...", "operation": {"kind": "...", "payload": {}, "consequence": "low", "artifact_code": "..."}}
```

Either key may be absent. Absence of `"operation"` means **no operation was requested this
turn** — no `Intent` is constructed, `Broker.dispatch()` is never called, `Gate`/`Executor` are
never invoked. Presence of `"operation"` means an `Intent` will be constructed and dispatched
exactly as today, with one new harness-level requirement: `artifact_code` is no longer optional
inside `"operation"`.

## 2. Rejected alternatives

**DESIGN DECISION**, reasoning below, evaluated against the assessment's own §20 comparison table
(re-verified against source, not merely trusted) and the ten requirements in the task prompt:

- **A — Action-only contract.** Rejected. Making every turn describe an action (even a
  manufactured no-op) is *closer* to today's already-failing prompt convention (assessment §3, §9,
  §21), not further from it — it does not solve requirement 1 (ordinary conversation must stay
  natural) or requirement 6 (message text must not become executable merely because the harness
  needs something to dispatch). It also risks pulling a conversational/no-op concept toward
  `Policy`/`Gate` if enforcement moves early enough to matter (assessment §20 row "Effect on
  core"), in tension with requirement 4.
- **B — First-class message-only turn.** Close to C, and shares C's core-independence property,
  but requires the model to choose between two distinct top-level turn *kinds* rather than one
  envelope with two independently optional fields. This is a real requirement-9 case (message
  only / operation only / message+operation) that B handles by branching *before* parsing, while C
  handles it by one schema with optional keys — strictly more uniform, and it is what
  `ParsedTurn.message` + `Intent` **already structurally look like today** at the harness layer
  (assessment §9's own conclusion: today `message` is "(C) optional metadata carried alongside an
  Intent, at the harness layer only"). Rejected in favor of C because C is the same idea made
  honest (operation truly optional, not always-present) rather than a new turn-kind taxonomy.
- **D — Current every-turn-execution model, made strict.** Rejected. This is the assessment's own
  §21 "smallest next experiment" framing (require `artifact_code` whenever `kind` is in
  `DEFAULT_ALLOWED_KINDS`), and it is *subsumed* by C's artifact_code requirement (§7/§8 below) —
  but D alone does nothing about requirement 1/2/6: it still forces every conversational turn to
  carry a `kind` and a manufactured inert `artifact_code`, the exact friction the assessment's §2
  empirical failures and §20 "Falsifiable downside" column call out for both A and D. D closes the
  artifact_code gap but not the conversational-naturalness or fake-attribution gaps; C closes all
  three with one change.

**INFERENCE** (matches the assessment's own §20 closing inference, now confirmed by re-reading
`ParsedTurn`'s docstring directly in this stage, not merely cited): C is not a new architectural
idea — it is today's actual harness-layer shape (`ParsedTurn.message` alongside an `Intent`,
`intent_parsing.py:26-37`), minus the current bug that `Intent`/`operation` is mandatory-in-form
but optional-in-content (`artifact_code=None` slipping through), made honest by making `operation`
optional-in-form and, when present, structurally required-in-content.

## 3. Layer ownership

| Concept | Owner | Change from today |
|---|---|---|
| Envelope shape (`message` / `operation` top-level keys) | `siphonophore_harness` (`intent_parsing.py`, `prompts.py`) | New |
| `Intent` (`kind`, `principal_id`, `intent_id`, `payload`, `consequence`, `artifact_code`) | `siphonophore_core.intent` | **Unchanged** — still a frozen dataclass with `artifact_code: str \| None = None`; the harness's stricter requirement is enforced *before* an `Intent` is even constructed, not by changing the type |
| Decision / Gate / Executor / Effect | `siphonophore_core` (`mediation.py`, `execution.py`) | **Unchanged** |
| `ParsedTurn` | `siphonophore_harness.intent_parsing` | `intent: Intent \| None` (was implicitly always-present); `message` unchanged |
| Message-only turn result | `siphonophore_harness.outcome` (new type, see §12) | New |

No concept crosses from harness into core. This directly satisfies requirement 4 (core stays
independent of conversational/model semantics) and requirement 5 (external harnesses, which import
`siphonophore_core` directly and never see `siphonophore_harness.intent_parsing`, are wholly
unaffected — verified by re-reading `siphonophore_core/intent.py`, `mediation.py`'s consumption
pattern via `broker.py`, and confirming no proposed change touches any file under
`siphonophore_core/`).

## 4. Target model-response schema

**DESIGN DECISION.** Top-level allowed keys: `{"message", "operation"}`. Both independently
optional. `"operation"`, when present, must decode to a JSON object with required field `"kind"`
and (new) required field `"artifact_code"`; allowed fields
`{"kind", "payload", "consequence", "artifact_code"}` — `"message"` is *not* allowed inside
`"operation"` (it now lives only at the envelope's top level; today's flat shape had it as a sibling
of `kind`/`artifact_code`, which is exactly the shape being replaced).

1. **Message only:**
   ```json
   {"message": "Tokyo is the capital of Japan."}
   ```
2. **Message + operation:**
   ```json
   {
     "message": "Sure, writing that now.",
     "operation": {
       "kind": "run_artifact",
       "payload": {},
       "consequence": "low",
       "artifact_code": "with open('/tmp/example.txt', 'w') as f:\n    f.write('hello')"
     }
   }
   ```
3. **Operation only** (permitted — a message is never required to accompany a real operation;
   forcing narration on every action turn is not something any requirement asks for):
   ```json
   {
     "operation": {
       "kind": "run_artifact",
       "payload": {"url": "https://example.invalid/x"},
       "consequence": "low",
       "artifact_code": "..."
     }
   }
   ```
4. **Invalid — operation present but missing required execution material:**
   ```json
   {"message": "I'll fetch that now.", "operation": {"kind": "run_artifact", "payload": {}, "consequence": "low"}}
   ```
   Fails closed with `IntentParseError` (`artifact_code` missing from `operation`) — **before**
   `Broker.dispatch()` is ever called. Note this fails *because* `"operation"` was explicitly
   present and malformed, never because a message-only response happened to look action-shaped.

**DESIGN DECISION**: `{}` (both keys absent) and `{"operation": null}` are both treated as
equivalent to message-only-with-no-text (`ParsedTurn(message=None, intent=None)`), not as errors —
a degenerate but harmless response, not a malformed one. `{"operation": {}}` (an explicit but empty
operation object) **is** treated as a malformed operation (missing `"kind"`) and fails closed,
because the model explicitly signaled "there is an operation" and then failed to describe one —
this is the requirement-10 boundary: explicit-and-malformed fails closed, absent-and-harmless does
not.

No Siphonophore internals (`token`, `decision`, `intent_id`) are exposed in this schema, unchanged
from today (`prompts.py:49-51`'s existing prohibition carries forward unchanged).

## 5. Message-only flow

For `"Good Morning"`, `"what is the capital of Japan?"`, and `"give me your operating context"` —
all three are handled identically under this contract:

| | Value |
|---|---|
| Model response shape | `{"message": "<answer text>"}` — `"operation"` absent |
| Operation exists? | No |
| `Intent` constructed? | **No** |
| `Gate` invoked? | **No** |
| `Executor` invoked? | **No** |
| REPL displays | The message text, plus **no** execution trace footer (no `execution_class`, no `intent_id`, no `[executed]`/`[...]` label at all — there is nothing truthful to put in one) |
| Attribution metadata | **None** — no `intent_id`, no `Decision`, no `Effect`, no `DecisionProjection` exist for this turn |

This is the direct fix for the assessment's §2 empirical observation: today, all three of these
prompts risk producing `[execution failed] same_process backend requires intent.artifact_code`
because the model is currently told "there is currently no way to send only a message"
(`prompts.py:52-53`, re-verified this stage) and must gamble on a manufactured no-op. Under this
contract, the model has a direct, structurally-supported way to say exactly this and nothing more
happens.

## 6. Message + operation flow

For `"inspect the contents of this URL"` (design only — no network access is taken in this stage):

| | Value |
|---|---|
| Response envelope | `{"message": "I'll fetch that now.", "operation": {"kind": "run_artifact", "payload": {"url": "..."}, "consequence": "low", "artifact_code": "<fetch code>"}}` |
| Operation object | As above; `kind="run_artifact"`, `consequence` model-declared (unchanged trust model — still not independently verified, `intent.py:21-26`'s own disclosed limitation, out of scope here) |
| `artifact_code` requirement | **Required**, non-null, non-empty — enforced by the harness parser before any `Intent` exists |
| Intent construction | Constructed by the (narrowed) `parse_intent()` from the `operation` sub-object, fresh `intent_id` minted exactly as today (`intent_parsing.py:86`, unchanged mechanism) |
| Gate | Invoked via `Broker.dispatch()` → `Gate.submit()`, **unchanged** — `ConsequencePolicy.evaluate()` still reads only `kind`/`consequence` (`policy.py:84-87`, re-verified) |
| Decision | Minted exactly as today; `artifact_digest` is now **always** a real hash for reference-harness-originated intents (never the empty-string case `mediation.py:107` produces for `artifact_code is None`), because the harness parser guarantees `artifact_code is not None` before `Intent` construction |
| Executor | Invoked exactly as today; the conditional digest check at `execution.py:168-174` now always executes (never skipped) for reference-harness-originated intents |
| Effect | Produced by `SameProcessBackend.run()` exactly as today — the stdout-capture gap (§12/§19 of the assessment) is explicitly **not** addressed by this design (see §21) |
| Message rendering | REPL prints `parsed.message` ("I'll fetch that now.") followed by the existing trace footer (`render_turn_result`), unchanged from today's successful-dispatch rendering |

## 7. Operation validation invariant

**DESIGN DECISION** — the target invariant, stated precisely:

> Any object that reaches `Broker.dispatch()` as an `Intent` from the reference harness's own
> parser already carries every field the execution path it will select structurally requires —
> specifically, `artifact_code is not None` for every `Intent` the reference harness constructs,
> because `SameProcessBackend`/`SeparateProcessBackend` (the only two backends `portable_profile()`
> registers, `composition.py:112-133`, re-verified) both unconditionally require it
> (`execution.py:107-108`, `129-130`).

This is enforced **once**, at the point where the `operation` sub-object is turned into an
`Intent` — i.e., inside the harness parser, not inside `Broker`/`Gate`/`Executor`/either backend.
It does not change what those layers accept; it changes what the reference harness is willing to
construct and hand to them in the first place.

**HARNESS LAYER responsible**: `siphonophore_harness.intent_parsing`, specifically the
operation-to-`Intent` construction step (today's `parse_intent`, narrowed — see §9). This is the
earliest point at which the invariant can be checked without touching `siphonophore_core`, and it
is a harness-owned, harness-only file already (not shared with any other consumer of
`siphonophore_core`). The backend remains, and must remain, a validator too (see §11) — this is
defense in depth, not a replacement.

## 8. `artifact_code` requirement

**CURRENT FACT, re-verified**: `siphonophore_core.intent.Intent.artifact_code` stays
`str | None = None` — this is `intent.py:38`, documented at lines 28-30 as a deliberate,
DESIGN.md-§8-grounded choice (empty-digest vs. fabricated-digest), not an oversight. **This design
does not change that field or its default.** The optionality lives in the *type*; the requirement
lives in the *harness parser that decides whether to construct one at all*.

**DESIGN DECISION**: inside the reference harness's `operation` schema, `artifact_code` moves from
`ALLOWED_FIELDS` (optional) to a member of a new, `operation`-scoped `REQUIRED_FIELDS = ("kind",
"artifact_code")`. `data.get("artifact_code")` is replaced with a presence-and-truthiness check:
missing, `null`, or `""` all raise `IntentParseError` inside operation-parsing — never inside
message-only parsing, which never reaches this code path at all (`"operation"` absent means this
function is never called).

## 9. Parser responsibilities

**DESIGN DECISION**, per the task's stated preference: `parse_intent`'s semantic meaning —
"parses an actual requested operation" — is **preserved**, not widened to parse arbitrary
conversation.

- New: `parse_turn(completion: str, principal_id: str) -> ParsedTurn` — the envelope-level parser.
  Owns: fence-stripping and `json.loads` (moved here from today's `parse_intent`, unchanged
  logic), top-level `{"message", "operation"} `field validation, message extraction, and *iff*
  `"operation"` is present and non-null, delegates to `parse_intent(operation_dict, principal_id)`.
- Narrowed: `parse_intent(operation: dict, principal_id: str) -> Intent` — takes an
  already-JSON-decoded `operation` object (not a raw completion string), applies the new
  `REQUIRED_FIELDS`/`ALLOWED_FIELDS` from §8, and constructs an `Intent` exactly as today
  (`intent_parsing.py:83-90`'s construction logic, unchanged). Called only when an operation
  genuinely exists — never on conversational text, satisfying requirement 6 directly.

`parse_intent` is a wrapped-by-a-higher-level-parser design (the task's second offered option),
not an action-only-called-conditionally variant of itself, because the envelope-level JSON
decoding and top-level shape validation is genuinely new work that doesn't belong inside a
function whose name and docstring are about operations specifically.

## 10. CognitiveLoop responsibilities

**DESIGN DECISION** — minimum change, not implemented here:

- `CognitiveLoop.step()` calls `self._model.complete(...)` unchanged.
- Replaces `parse_intent(completion, principal_id)` with `parse_turn(completion, principal_id)`.
- `self.last_message = parsed.message` — unchanged line, unchanged meaning.
- Branches purely mechanically on `parsed.intent is None`:
  - **None** → no `Broker.dispatch()` call at all; append an honest history entry (e.g.
    `{"role": "effect", "content": "no operation requested this turn"}`, not a fabricated Effect
    description); return a `MessageOnlyResult` (§12).
  - **Not None** → `effect = self._broker.dispatch(parsed.intent, authority=self._authority)`,
    unchanged; append history and return exactly as today.

`CognitiveLoop` does not decide *whether an operation should exist* — that judgment is entirely the
model's (expressed through the envelope) and the parser's (expressed through validation). The loop
only asks "does `parsed.intent` exist," never "should it." This preserves the file's existing
structural-proof property (`loop.py:14-17`'s docstring, re-verified: no effect-producing stdlib
import, no capability beyond `broker.dispatch()`) — the branch added is a `None` check on a value
already fully computed by the parser, not new authority or policy logic.

## 11. Broker/Gate/Executor responsibilities

**DESIGN DECISION**: **unchanged**, confirmed unnecessary to touch:

- `Broker.dispatch()` (`broker.py:53-64`, re-verified) still always mints a `Decision` via
  `Gate.submit()` before calling `Executor.execute()`, on every path it is invoked on. This design
  does not add a new `Broker` code path for "no operation" — it ensures `Broker.dispatch()` is
  simply never *called* for a message-only turn, at the `CognitiveLoop` layer (§10), not inside
  `Broker` itself.
- `Executor.execute()`'s conditional digest check (`execution.py:168-174`) and each backend's own
  `if intent.artifact_code is None: raise ExecutionError(...)` (`execution.py:107-108`, `129-130`)
  are **kept, not removed**, even though the reference harness's own parser now guarantees
  non-`None` `artifact_code` for every `Intent` it constructs. This is required by requirement 5:
  `siphonophore_core` must remain safe and correct for any caller, including one that never uses
  this harness's stricter parser and constructs an `Intent(artifact_code=None)` directly (as
  `tests/test_execution.py:189-193` already does, and continues to do — that test is unaffected by
  this design). The harness-level invariant (§7) is additive defense-in-depth at an earlier layer,
  not a replacement for the backend's own check.

## 12. Outcome/result semantics

**DESIGN DECISION**, answering the task's explicit either/or: **message-only is outside
`OutcomeCategory` entirely, *and* a new harness-level non-execution result type is introduced** —
these are not mutually exclusive, and both are needed.

- `siphonophore_harness.outcome.OutcomeCategory` (`outcome.py:88-101`, re-verified) stays exactly
  as it is — a closed enum over `Broker.dispatch()`'s own outcomes (`EXECUTED`, `INPUT_REJECTED`,
  `AUTHORITY_REJECTED`, `DENIED`, `INTEGRITY_REJECTED`, `BACKEND_UNAVAILABLE`, `IDENTITY_REJECTED`,
  `EXECUTION_FAILED`). None of these apply to a turn where `Broker.dispatch()` was never called —
  applying any of them would be exactly the false classification the task prohibits.
- New: `MessageOnlyResult` (frozen dataclass, `outcome.py`, alongside `DispatchResult`) —
  `message: str | None` only. **No** `intent_id`, **no** `execution_class`, **no** `decision`, **no**
  `detail`. Structurally incapable of carrying fake attribution, by omission rather than by a null
  placeholder (requirement 7).
- `classify_outcome()` (`outcome.py:104-146`, re-verified) is **unchanged** — its signature stays
  `DispatchResult | BaseException`. It is simply never called on a `MessageOnlyResult`; the REPL
  (or any caller) branches on `isinstance(result, MessageOnlyResult)` *before* ever reaching
  `classify_outcome()`, so passing the wrong type there is a caller bug the type system already
  discourages, not a case `classify_outcome()` needs to handle.
- `CognitiveLoop.step()`'s return type becomes `DispatchResult | MessageOnlyResult`.

## 13. Attribution semantics

**DESIGN DECISION**, directly satisfying requirement 7:

- **No operation → no fake execution provenance**: a message-only turn produces a
  `MessageOnlyResult` carrying only the message text. No `intent_id` is minted (`parse_intent` is
  never called, so `uuid.uuid4()` at `intent_parsing.py:86` never runs for this turn), no `Decision`
  exists, no `Effect` exists, no `DecisionProjection` exists. There is nothing to render as
  attribution because nothing was authorized or executed.
- **Operation → attribution preserved exactly as today**: `intent_id`, `Decision`
  (`permitted`, `execution_class`, `authority_id`, `order_id` via `DecisionProjection`), and
  `Effect` all flow through `Broker.dispatch()` unchanged (§11). Nothing about this design touches
  `Gate.submit()`'s HMAC minting or `Executor.execute()`'s re-verification.

## 14. Profile-capability prompt treatment

**DESIGN DECISION**: **combine, narrowly**, in the same implementation stage as the envelope
change — not as a separate stage.

Reasoning: the new contract is *stricter* about what a well-formed `operation` object must contain
(§8). If the model-facing prompt text still describes `"privileged"` as running "under a separate,
real OS identity" (`prompts.py:32-34`, re-verified unchanged, static, profile-independent) while the
active `portable_profile()` registers no `uid_cgroup` backend at all (`composition.py:112-133`,
re-verified — `execution_classes` is built directly from `backends.keys()`, no `uid_cgroup` entry),
the *same class of gap* the assessment's §16/§18 identified for capability text reappears for the
new, stricter `operation` schema: a model could honestly follow the new contract, name
`"consequence": "privileged"`, and still hit `NoBackendRegisteredError` — an authorized-but-
unexecutable turn, structurally identical to the artifact_code gap this design closes, just at the
`consequence`/execution-class boundary instead of the `artifact_code` boundary. Since both problems
live in `prompts.py` and both are "what the model may legitimately claim it can request," closing
one without the other in the same stage would leave a known, structurally analogous gap open on
purpose.

**Scope of the combination, kept minimal**: only the prose *describing* consequence-tier
capabilities becomes profile-derived (e.g. a `build_system_prompt(profile: ExecutionProfile) -> str`
replacing the module-level `DEFAULT_SYSTEM_PROMPT` constant, rendering the consequence/execution-
class vocabulary line from `profile.policy_mapping`/`profile.execution_classes` — mirroring
`render_startup_banner`'s existing pattern, `repl.py:116-131`, re-verified). The `operation` JSON
**schema itself** (`kind`, `payload`, `consequence`, `artifact_code` field names) stays
profile-independent, harness-owned, and unchanged by this — parsing does not take a profile
argument; only prompt-text generation does. This does **not** touch `ConsequencePolicy`,
`ExecutionProfile`, or any backend (requirement stands: substrate behavior itself untouched).

## 15. Backward compatibility

**DESIGN DECISION / CURRENT FACT**: this is a **breaking change to the completion wire format**,
by necessity — there is no way to make `artifact_code` structurally required and `operation`
structurally optional without moving it out of the flat top-level shape `parse_intent` accepts
today. Concretely:

- Every existing completion of the shape `{"kind": ..., "artifact_code": ..., "message": ...}`
  (flat) is no longer valid; it must become
  `{"message": ..., "operation": {"kind": ..., "artifact_code": ..., ...}}` (nested).
  `ScriptedModel`-driven tests supplying flat completions will need their fixture completions
  updated.
- `parse_intent`'s signature changes (operation-dict in, not completion-string in) — any direct
  caller of `siphonophore_harness.intent_parsing.parse_intent` (inside this repo: none found
  outside `intent_parsing.py`'s own tests and `loop.py`, both re-verified this stage; **OPEN
  QUESTION**: whether any out-of-tree harness imports this specific function directly, which this
  stage has no visibility into) needs updating.
- `CognitiveLoop.step()`'s return type widens (`DispatchResult | MessageOnlyResult`) — any caller
  currently assuming every `step()` call returns something `Effect`-shaped (`intent_id`,
  `execution_class`, `detail`) needs an `isinstance` check added. `examples/repl.py`'s `main()` is
  the only in-repo caller (re-verified).

This is a deliberate, scoped breaking change to the reference harness's own model contract — it
does not touch `siphonophore_core`'s public API at all (§3), so it is invisible to any consumer
that only imports `siphonophore_core`.

## 16. External-harness implications

**None to `siphonophore_core`.** Re-verified this stage: `Intent`, `Effect`, `Gate`, `Policy`,
`ConsequencePolicy`, `Executor`, both backends, and `Decision` are untouched by every change listed
in §17 below. An external harness building its own parser directly against `siphonophore_core`
(never importing `siphonophore_harness`) sees zero change — it can already construct an `Intent`
with `artifact_code=None` today and will still be able to after this design ships (§11), and it can
already choose its own message/no-op convention today (`siphonophore_core` has no concept of
`message` at all — `intent.py`, re-verified, has no such field).

**Some, to `siphonophore_harness`**: anything downstream of `siphonophore_harness.intent_parsing`
or `siphonophore_harness.loop` inside or outside this repo needs the wire-format and return-type
updates in §15.

## 17. Required source changes for implementation (not made in this stage)

1. `siphonophore_harness/intent_parsing.py` — new `parse_turn()`; narrowed `parse_intent()`
   (dict-in, not string-in; new `REQUIRED_FIELDS`/`ALLOWED_FIELDS` for the `operation` sub-object);
   `ParsedTurn.intent: Intent | None`.
2. `siphonophore_harness/prompts.py` — rewrite `DEFAULT_SYSTEM_PROMPT` for the envelope shape;
   add `build_system_prompt(profile: ExecutionProfile) -> str` per §14 (or an equivalent
   profile-parameterized constructor), replacing the static consequence-capability prose with text
   derived from `profile.policy_mapping`/`profile.execution_classes`.
3. `siphonophore_harness/loop.py` — `CognitiveLoop.step()` per §10; return type
   `DispatchResult | MessageOnlyResult`.
4. `siphonophore_harness/outcome.py` — new `MessageOnlyResult` dataclass; `classify_outcome()`
   itself unchanged.
5. `examples/repl.py` — `main()`'s call to construct the model's `system` prompt switches to
   `build_system_prompt(profile)`; `render_turn`/rendering path branches on
   `isinstance(result, MessageOnlyResult)` to omit the trace footer for message-only turns
   (§5/§13).
6. Test fixtures — any `ScriptedModel` completion strings in
   `tests/test_harness_intent_parsing.py`, `tests/test_harness_loop.py`, `tests/test_repl.py` using
   today's flat shape need updating to the nested envelope.

No `siphonophore_core/*` file is listed here — none require a change for this design.

## 18. Required tests (not written in this stage)

- `parse_turn()`: message-only → `ParsedTurn(message=..., intent=None)`; message+operation with
  valid `artifact_code` → both populated; operation-only (no message) → `intent` populated,
  `message=None`; `{}` and `{"operation": null}` → both fields `None`, no error; `operation`
  present but missing `artifact_code` → `IntentParseError`; `operation: {}` (empty) →
  `IntentParseError` (missing `kind`); `operation` not a JSON object (e.g. a string) →
  `IntentParseError`; unknown top-level field → `IntentParseError`; unknown field inside
  `operation` → `IntentParseError` (same check as today, re-scoped).
- `CognitiveLoop.step()`: message-only completion never calls `broker.dispatch()` (spy/mock
  `Broker` asserting zero calls) and returns `MessageOnlyResult`; message+operation completion
  dispatches exactly as today (regression test preserving the pre-existing executed-path
  behavior); history is updated honestly for a message-only turn (no fabricated Effect
  description).
- Regression: `tests/test_execution.py::test_same_process_backend_requires_artifact_code` (core,
  unmodified) continues to pass unchanged — backend-level defense-in-depth is preserved (§11).
- New: a harness-level test constructing a completion equivalent to "give me your operating
  context" under the new contract (`{"message": "..."}`, no `operation`) and asserting `Gate`/
  `Executor`/`Broker.dispatch()` are never reached — directly falsifying, for the reference
  harness specifically, the exact failure path the assessment traced in its §8 (core itself
  remains capable of the old failure mode if a caller constructs an `Intent` by hand, which is
  correct and unchanged, per requirement 5).
- New (§14): a test that `build_system_prompt(portable_profile())`'s output does not mention
  `uid_cgroup`/`"privileged"` execution as available when no such backend is registered — or, if it
  still describes `privileged` as a *concept*, that it doesn't claim it as *deliverable* by the
  active profile.

## 19. Acceptance criteria

1. A completion of the form `{"message": "..."}` never results in a `Broker.dispatch()` call.
   (req. 2)
2. Every `Intent` the reference harness's `parse_intent()` constructs has `artifact_code is not
   None`. (req. 3)
3. No file under `siphonophore_core/` changes. (req. 4, 5)
4. `message` text is never interpreted as, or copied into, any `Intent` field. (req. 6)
5. `MessageOnlyResult` carries no `intent_id`/`execution_class`/`decision`/`detail` fields; every
   `DispatchResult` still carries all of them exactly as today. (req. 7)
6. `Gate.submit()`, `Executor.execute()`, both backends' `run()` methods, `Policy.evaluate()`, and
   `Decision`/`Effect`'s field shapes are byte-for-byte unchanged from
   `70a45e9be6f503bdc86de4516f163ce0d220ed48`. (req. 8)
7. A hand-built `Intent(artifact_code=None)` submitted directly to `Gate`/`Executor` (bypassing
   `siphonophore_harness` entirely) behaves exactly as it does today (parses/authorizes, backend
   rejects). (req. 9, external-harness independence)
8. `operation: {}` (or any operation object missing `kind` or `artifact_code`) raises
   `IntentParseError`; `{}` / absent `operation` / `operation: null` do not. (req. 10)

## 20. Falsification / counterexamples

- Requirement 2 is falsified by any code path in which a `ParsedTurn` with `intent is None`
  reaches `Broker.dispatch()` — i.e. any place `CognitiveLoop.step()` (or any future caller) calls
  `dispatch()` without first checking `parsed.intent is not None`.
- Requirement 6 is falsified if any code constructs an `operation` object, an `Intent`, or a `kind`
  value from the contents of `"message"` text.
- Requirement 7 is falsified if `MessageOnlyResult`, or anything rendering it, exposes an
  `intent_id`, `execution_class`, or `decision`-shaped field (even as `None`/empty placeholders —
  the design calls for omission, not nulled-out presence, so a reader cannot mistake "no
  operation" for "operation that produced empty attribution").
- Requirement 3/§7's invariant is falsified by any `Intent` reaching `Broker.dispatch()` from
  `siphonophore_harness.intent_parsing.parse_intent()` with `artifact_code is None` — this is
  specifically a claim about the reference harness's own parser, not about `siphonophore_core`,
  which correctly continues to accept such an `Intent` from any other caller (§11, req. 5).
- Requirement 10 is falsified either by a message-only completion ever raising `IntentParseError`,
  or by a completion with `operation` present but missing `artifact_code` being silently accepted
  (constructing an `Intent` with `artifact_code=None` from the reference harness's own parser would
  itself falsify §7/§8's invariant simultaneously).
- §14's decision is falsified if `build_system_prompt()`'s output is identical regardless of which
  `ExecutionProfile` is passed to it (i.e., if the "combine" decision is not actually implemented
  as anything more than a renamed constant).

## 21. Explicitly deferred issues

Unchanged from the assessment's own scope, restated here for this stage's own boundary:

1. `same_process` stdout capture / terminal-output escape (assessment §12/§19) — **not addressed**;
   `SameProcessBackend.run()`'s `exec()` call is untouched by every change in §17.
2. Non-JSON completion retry/repair (assessment §15) — **not addressed**; `parse_turn()`'s
   `json.loads()` failure mode is unchanged from today's `parse_intent()` (still raises
   `IntentParseError`, still no retry inside `CognitiveLoop`).
3. Kubernetes / `uid_cgroup` / `k8s_pod` backends — **not addressed**; `execution_uid_cgroup.py`
   and `execution_k8s.py` are untouched, and §14's profile-derived prompt text change describes
   only what a profile *has registered*, never adds a new backend.
4. Authority/delegation semantics (`Authority`, `Gate.issue_order`/`grant_root_authority`/
   `delegate`) — **not addressed**; `Broker.dispatch()`'s `authority` parameter is threaded through
   completely unchanged (§11).
5. Stage 5 documentation (whatever that stage's own scope is defined as elsewhere) — **not
   addressed** here.

---

## Validation

Full portable suite, run unmodified on `design/reference-harness-turn-contract` (no source/test
file touched this stage): expected baseline **224 passed, 43 skipped**.

`git diff --name-only` against the branch point contains only
`docs/REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md`.

No network access, no Anthropic API call, no Kubernetes, no `agent-vm`, no `~/research` access
occurred at any point in this stage.

## Adversarial review

1. **Is the selected contract justified from the investigation evidence?** Yes — §1/§2 ground the
   choice in the assessment's own §9 conclusion (today's shape is already closest to C) and §20
   comparison table, re-verified against source rather than merely cited.
2. **Are model/conversation semantics kept outside core?** Yes — §3/§16; no `siphonophore_core`
   file appears in §17's change list.
3. **Does a message-only response avoid creating fake execution attribution?** Yes — §12/§13;
   `MessageOnlyResult` structurally omits every attribution field rather than nulling them out.
4. **Does every operation entering mediation satisfy its structural requirements?** Yes for the
   reference harness's own parser (§7/§8); core/backends independently continue to enforce it too
   (§11), unchanged, for any other caller.
5. **Is `artifact_code` validation placed at the correct layer?** Yes — harness parser (§7), not
   moved into `Gate`/`Policy`/core, and not removed from the backends (defense in depth preserved,
   §11).
6. **Are Gate/Executor semantics preserved?** Yes — §11, byte-for-byte, confirmed by re-reading
   both files this stage; §19 acceptance criterion 6 makes this falsifiable.
7. **Can external harnesses still use Siphonophore core independently?** Yes — §16, re-verified
   directly against `intent.py`/`mediation.py`/`execution.py`, none of which change.
8. **Is `OutcomeCategory` kept semantically honest?** Yes — §12; the enum is untouched and never
   applied to a non-`Broker.dispatch()` outcome.
9. **Is the active-profile prompt mismatch addressed or explicitly deferred?** Addressed, narrowly
   and deliberately combined into this stage's scope, with reasoning given (§14) rather than
   silently bundled or silently dropped.
10. **Are stdout capture and non-JSON repair still separate?** Yes — §21, items 1 and 2, explicitly
    named as untouched.
11. **Were alternatives A–D genuinely considered?** Yes — §2, each scored against the assessment's
    own §20 table plus the task's ten requirements, not dismissed by aesthetics.
12. **Are counterexamples/falsification criteria included?** Yes — §20, one per requirement that
    this design's own acceptance criteria (§19) claim to satisfy.
13. **Did any source/test code change?** No — verified by `git diff --name-only` in Validation
    above; this document is the sole change.
14. **Was `~/research` untouched?** Yes — never accessed, referenced, or read this stage.

No unsupported claim was identified requiring correction before commit.
