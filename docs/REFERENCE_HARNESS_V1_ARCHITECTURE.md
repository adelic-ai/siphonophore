# Reference Harness V1 — Architecture

**Status: V1 IMPLEMENTATION, ready for owner inspection.** This document is the single reference
for what was actually built on `work/reference-harness-v1`, superseding the narrower scope of
`docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md` (still accurate for the continuation state machine
specifically) and reconciling the open items from `docs/REFERENCE_HARNESS_V1_HORIZON.md`. It is
written after implementation, not before — every claim here is checked against real code and real
tests in this repository, not aspirational.

## North Star, Restated

Siphonophore is a platform-independent execution-security SDK. It is not a Claude harness, not an
agent framework, not this one enterprise workflow. The reference harness is one consumer,
demonstrating one useful composition — not a claim about the only correct one.

The reference harness's own north star: the front-end interaction should feel like an ordinary
modern conversational interface. The unusual property is not the UX — it's that every real effect
the model requests is explicit and independently mediated. Users should not need to understand
`Intent`/`Decision`/`Effect`/`GateViolation` subclasses/JSON envelope mechanics to use it; those
remain available through `--verbose` and the JSONL event log.

## The First Reference Agent: a Planning/Cognitive Workspace

`examples/repl.py --profile planning` (the default) is a long-horizon conversational agent that
**cannot** edit the repository, run shell commands, or execute arbitrary code — not because it is
instructed not to, but because the active `ExecutionProfile` (`siphonophore_harness/composition.py`,
`planning_profile()`) registers no code-execution backend and its `Policy`
(`policy.KindExecutionPolicy`) has no mapping for `run_artifact`/`write_file` at all. Two
independent refusal layers, neither dependent on the model's cooperation:

1. `Gate.submit()` returns `permitted=False` for either kind (not in the policy's mapping).
2. Even if it somehow didn't, `Executor` has no backend registered to dispatch to
   (`NoBackendRegisteredError`).

It may converse, reason, plan, and inspect the repository through three typed, mediated,
root-confined observational operations (`siphonophore_core/execution_readonly.py`): `read_file`,
`list_directory`, `search_repository`. Every one of these is a real `Intent` → `Gate` → `Decision`
→ `Executor` → `Effect` cycle, identical in kind to the code-execution path — the operation surface
changed, the mediation boundary did not.

`--profile portable` (the original `same_process`/`separate_process` code-execution profile)
remains available for comparison/testing, unchanged in its own semantics.

## Typed Operation Surface

**Implemented**: `read_file(payload.path)`, `list_directory(payload.path?)`,
`search_repository(payload.pattern, payload.path?)` — each a narrow `ExecutionBackend`
(`execution_readonly.py`) confined to a `root` directory supplied at profile-construction time
(never from the model), with real, symlink-aware path containment and bounded output (100,000
bytes/2,000 entries/500 matches, each truncated with an explicit marker, never silently).

**Deliberately not implemented**: `fetch_url` (network I/O — SSRF and timeout/abuse-boundary
questions deserve their own design pass, not a rushed addition here) and any generic MCP/tool
transport (MCP is a possible interface, never Siphonophore's identity — nothing here assumes or
requires it). Neither omission blocks any of the 17 reference acceptance scenarios below.

**Routing.** `siphonophore_core/policy.py` gained `KindExecutionPolicy` — routes `execution_class`
directly by `intent.kind`, ignoring `consequence` entirely. This is the one place a
consequence-tier model (`"low"/"high"/"privileged"`) genuinely doesn't fit: a `read_file`
operation's execution class is what it is, not a declared risk tier. `Policy` was already a
pluggable ABC for exactly this kind of extension (DESIGN.md §2); this is not a new capability, it's
the second concrete implementation of an interface that always supported more than one.

`Intent.artifact_code` is now required for `CODE_BEARING_KINDS` (`run_artifact`, `write_file`) and
**forbidden** for every other kind (`intent_parsing.py`) — a typed operation's behavior is fixed by
its backend, never by caller-supplied code, so attaching code to one would be silently ignored and
misleading. Enforced at the harness parser, before any `Intent` exists.

## Core Mediation Invariant (Unchanged)

- A conversational-only turn creates no `Intent`, invokes no `Gate`, mints no `Decision`, invokes no
  `Executor`. Verified: `test_message_only_completion_never_calls_broker_dispatch` and equivalents,
  using a spy `Broker` that raises `AssertionError` if `dispatch()` is ever called.
- Every operation, in every continuation cycle, independently traverses
  `Gate.submit() → Executor.execute()`. No cycle reuses, batches, or caches a prior `Decision`.
- A prior authorized operation never implicitly authorizes another — each dispatch is checked from
  scratch against the same session-level `Authority` (or lack of one), regardless of what happened
  earlier in the same turn.
- The cognitive loop (`loop.py`) still imports no effect-producing stdlib module
  (`test_harness_structural_proof.py`, mechanically enforced) — `event_sink`, like `Model` and
  `Broker` before it, is a plain interface CognitiveLoop calls without itself gaining a second path
  to a real-world effect.
- Compiling a `WorkOrder` never touches `Broker.dispatch()` — verified directly
  (`test_work_order_finalization_never_calls_broker_dispatch`, a counting-broker spy proving zero
  calls, not merely that the return value looks right).

## Transactional History (the "Dangling Failed Turn" Fix)

**The defect** (confirmed via a real Mac trial and reproduced deterministically in this arc): the
pre-V1 `CognitiveLoop.step()` appended the user's message to `self.history` unconditionally, before
`model.complete()`/`parse_turn()` had a chance to fail. A parse failure appended nothing further,
leaving a dangling, unresolved `"user"` entry. The next, unrelated turn's own `"user"` entry landed
immediately after it with no assistant reply in between — two consecutive `"user"`-role messages
sent to the model, which then addressed both, exactly matching the empirically observed
"model answers the new question but also spontaneously continues discussing the failed
investigation" symptom.

**The fix.** `CognitiveLoop.step()` stages this turn's history additions in a local
`working_history` list and commits them into `self.history` only at the moment the turn produces
something real and truthful:

- a terminal message (with or without a `WorkOrder`),
- the per-turn operation bound being reached, or
- a real `Broker.dispatch()` attempt (successful or not — even a denial or a failure is a real
  mediation event worth keeping).

A turn that fails before any of those (a model-transport exception, or every parse-retry attempt
exhausted with nothing else having happened) is rolled back **entirely**, including the user's own
message — `self.history` is never touched. Once anything commits, it commits permanently for that
turn: a real operation, once it happened, is never later erased by a subsequent failure in the same
turn, and nothing is ever fabricated (no invented assistant reply) merely to keep roles alternating.

Verified exhaustively (`tests/test_harness_transactional_history.py`) against ten scenarios:
model-transport failure, empty/malformed response, parse failure (all three before any operation —
roll back completely), denied operation, backend unavailable, execution failure (all three commit
and are recoverable), operation-then-continuation-failure, multiple-operations-then-final-failure
(the real operations survive), integrity rejection (fails closed, the attempt itself still
committed), and operation-bound exhaustion (commits with an honest limit marker). A 15-turn mixed
long-session test confirms this holds up over many turns, not just one.

## Bounded Malformed-Output Robustness

A completion that fails `parse_turn()` gets up to `max_parse_retries_per_turn` (default 2, a plain
integer, never read from or influenced by the model) further chances to self-correct **within the
same turn** — never a new user turn, never a redispatched operation (a parse failure happens before
any `Intent` exists, so there is nothing to redispatch). Reuses the transactional history model with
no special-casing: retries that never resolve into something real roll back together with the rest
of the turn; a retry after a real operation already committed lands in history immediately and
truthfully. Every attempt is logged (`model.failure` per failure).

## Operation Continuation (Preserved, Re-verified Against the New Surface)

The bounded multi-cycle contract from `docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md` is unchanged
in shape and re-verified end to end against the new typed-operation surface
(`test_continuation_check`-equivalent coverage folded into `test_harness_transactional_history.py`
and `test_harness_session_log.py`'s end-to-end JSONL test): operation → mediation → captured,
bounded result → fed back to the model → model answers or requests another operation → final
answer, with a compact trace after it. **Superseded by the V1.1 stabilization addendum at the end
of this document**: `max_operations_per_turn` (default 4) was V1's only bound on this loop and
doubled as its logical-completion signal, which V1.1 replaced with a real completion signal plus
two independently-scoped anomaly backstops. The model-context truncation bound (4,000 characters,
independent of the 100,000-character backend capture bound) is unchanged.

## WorkOrder Model

A harness-only concept (`siphonophore_harness/work_order.py`) — never routed through
`Broker.dispatch()`, grants no authority, launches no workers. The model-facing envelope
(`intent_parsing.py`) gained a third, independently-optional top-level field, mutually exclusive
with `"operation"`:

```json
{"message": "...", "work_order": {
  "status": "draft" | "final",
  "objective": "...",
  "prompt": "...",
  "requirements": ["..."], "constraints": ["..."], "acceptance_criteria": ["..."],
  "context": ["..."], "prohibited_scope": ["..."], "expected_artifacts": ["..."],
  "capability_requirements": ["..."], "data_requirements": ["..."],
  "network_requirements": ["..."], "credential_requirements": ["..."],
  "isolation_requirements": ["..."], "resource_expectations": "...",
  "decomposition_suggestions": ["..."]
}}
```

`status`/`objective`/`prompt` are required; everything else is optional and defaults to empty.
`work_order_id` is always harness-minted (fresh `uuid4`), never accepted from the completion — the
same discipline already applied to `intent_id`.

**Finalization point, precisely**: whenever the model's completion includes a `work_order` object
with `"status": "final"`. This is entirely the model's (and, behind it, the user's) judgment call —
`CognitiveLoop` does not decide when a session has converged; it only classifies what the completion
said (`work_order.draft` vs. `work_order.finalized` event) and returns it as `TurnResult.work_order`.
The system prompt (`prompts.py`) explicitly instructs the model not to send `"final"` merely because
a lot of conversation happened.

**Relationship among transcript, WorkOrder, Constructor, and future workers.** The JSONL transcript
(below) is provenance — a durable record of everything that happened in the session, including every
draft. The `WorkOrder` is the execution specification — self-contained enough that a future
Constructor should not need to replay the transcript to know what to build. The Constructor itself
is **not implemented in this arc** (explicitly out of scope, per instruction) — it is the
still-future component that would read a finalized `WorkOrder` and decide how to realize it,
possibly as multiple bounded workers with independently chosen models, credentials, filesystems,
networks, isolation, and lifetimes. Nothing here grants any of that; `capability_requirements`/
`network_requirements`/`credential_requirements`/`isolation_requirements` are the planning agent's
*requests*, never authority. `requested capability != granted authority` is enforced structurally by
omission: nothing in this codebase reads a `WorkOrder`'s requirement fields and turns them into a
`Broker.dispatch()` call, an `Authority`, or a credential of any kind.

## Durable JSONL Event Log

`siphonophore_harness/session_log.py` — `EventLog`, a thin, additive, model-neutral append-only
JSONL writer. **Architecturally separate from `CognitiveLoop`'s own effect-freedom proof**: writing
this file is control-plane/infrastructure behavior, never a model-requested operation.
`CognitiveLoop` never constructs an `Intent` for its own logging (verified:
`test_event_sink_receives_nothing_when_none_configured` and the transactional-history event-capture
tests, which show `Broker.dispatch()` call counts are entirely independent of how many events were
emitted). `CognitiveLoop` only ever calls a plain `event_sink(dict)` callable — the actual file I/O
lives in `session_log.py`, which is *not* one of the files
`test_harness_structural_proof.py`'s "no effect-producing stdlib import" check covers, so the
structural guarantee stays meaningful: the only way for `CognitiveLoop` to reach the outside world
remains an object it was explicitly handed (`Model`, `Broker`, now `event_sink`), never a direct
`os`/`pathlib`/socket import inside `loop.py` itself.

**Schema** (`schema_version=1`): every event carries `schema_version`, `event`, `session_id`,
`timestamp` (ISO-8601 UTC), plus type-specific fields. Event types actually emitted:

```
session.started, session.completed      (main(), REPL-owned session lifetime)
user.message                            (turn_id, content)
model.response                          (turn_id, cycle_id, completion, block_types,
                                          text_block_count, retained_text_length, stop_reason,
                                          input_tokens, output_tokens -- diagnostics fields present
                                          only when the driving Model provides them)
model.continuation                      (turn_id, cycle_id -- marks a 2nd+ cycle's model call)
model.failure                           (turn_id, cycle_id, error_type, error_message)
operation.requested                     (turn_id, cycle_id, intent_id, kind, consequence)
mediation.decision                      (turn_id, cycle_id, intent_id, category, execution_class,
                                          authority_id, order_id)
operation.result                        (turn_id, cycle_id, intent_id, category, detail, reason)
turn.completed                          (turn_id, operation_count, exhausted[, work_order_id])
turn.failed                             (turn_id, reason[, error_message])
work_order.draft, work_order.finalized  (turn_id, cycle_id, work_order -- the FULL WorkOrder dict,
                                          deliberately exempt from the generic per-field text bound:
                                          self-containment is the point)
```

**Example sequence, one observational operation** (from
`test_end_to_end_turn_produces_a_coherent_correlated_jsonl_stream`, real output, not illustrative):

```
user.message        {turn_id: t1, content: "do something"}
model.response       {turn_id: t1, cycle_id: c1, completion: "{...operation...}", ...}
operation.requested  {turn_id: t1, cycle_id: c1, intent_id: i1, kind: "run_artifact", consequence: "low"}
mediation.decision   {turn_id: t1, cycle_id: c1, intent_id: i1, category: "executed", execution_class: "same_process", ...}
operation.result     {turn_id: t1, cycle_id: c1, intent_id: i1, category: "executed", detail: {...}}
model.continuation   {turn_id: t1, cycle_id: c2}
model.response       {turn_id: t1, cycle_id: c2, completion: "{...message...}", ...}
turn.completed       {turn_id: t1, operation_count: 1, exhausted: false}
```

**Correlation.** `session_id` ties every event in one process lifetime together. `turn_id` ties
every event within one `CognitiveLoop.step()` call. `cycle_id` ties one model round-trip and
whatever it produced. `intent_id` ties `operation.requested`/`mediation.decision`/`operation.result`
for one dispatch. All four are plain, non-secret UUIDs — sufficient to reconstruct, from the JSONL
alone, exactly which model call produced which operation and what the Gate decided about it, without
ever exposing a `Decision` token, an API key, or bearer authority material (`session_log.py`'s
`_FORBIDDEN_EVENT_KEYS` filter, defense in depth alongside the discipline `loop.py`'s own emit call
sites already follow).

**Redaction/bounding.** Any free-text field is capped at 4,000 characters (`_MAX_EVENT_FIELD_CHARS`,
`session_log.py`) with an explicit truncation marker — independent of, and separate from, both the
backend capture bound (100,000 characters, `execution.py`) and the model-context bound (4,000
characters, `loop.py`; the two numbers coincide by choice, not by shared implementation). `WorkOrder`
payloads are exempt from this bound, deliberately — self-containment is the entire point of a
WorkOrder, so truncating one in the audit log would defeat its own purpose. Hidden model reasoning
(the content of a `"thinking"`-type block) is never captured anywhere in this codebase in the first
place — `model_anthropic.py`'s own extraction only ever reads `.text` from `type == "text"` blocks —
so there is nothing to accidentally log.

## AgentWatch Interoperability — Assessment, Not Validation

**No local AgentWatch implementation was found outside `~/research`** to validate the event schema
against empirically (searched the filesystem; none present in this environment). This section is
therefore a **design-level assessment**, not an empirical compatibility proof, and should be
labeled as such to the owner.

The schema was deliberately shaped around the generic semantic structure a factory-model harness
observer needs — user interaction, model output, explicit tool/operation request, tool/operation
result, stable ordering, timestamps, stable correlation IDs — rather than cloning Claude Code's own
private JSONL schema (inspected read-only, structurally: session snapshots, bridge-session IDs,
owner-account/org UUIDs — clearly proprietary session-management concepts, not a generic event
model, and deliberately not imitated here). `event`, `turn_id`, `cycle_id`, `intent_id`,
`session_id` are plain, self-describing strings; nothing in the schema requires knowing what
Siphonophore's own internal types are to parse it. The expected residual adapter work for a real
AgentWatch integration: mapping this schema's `event` values onto whatever AgentWatch's own ingest
format calls them (a field-renaming shim, not a semantic translation) — genuinely thin, but this
claim is unverified against a real AgentWatch parser and should be treated as a hypothesis pending
that verification.

## Model-Adapter Architecture

`siphonophore_harness/model.py` defines the only interface a cognitive loop depends on:
`Model.complete(messages: list[dict]) -> str`. No provider-specific type appears in this signature.
`model_anthropic.py` is the one live adapter (`AnthropicAPIModel`), and is the **only** file in
`siphonophore_harness/` permitted to mention Anthropic/Claude-specific vocabulary — verified
mechanically (`tests/test_harness_model_agnosticism.py`, mirroring
`test_core_no_k8s_vocabulary.py`'s own scanning convention): every conceptual module (`loop.py`,
`intent_parsing.py`, `outcome.py`, `broker.py`, `work_order.py`, `session_log.py`, `composition.py`,
`prompts.py`) is scanned for `anthropic`/`claude`/`openai`/`gemini`/`thinking_block`/`tool_use`/
`tool_result` and asserted clean. Thinking blocks, Claude's own message-role mapping quirks (the
`"effect"` → Anthropic-`"user"` role remapping, `model_anthropic.py`'s own disclosed alternation
limitation), and the raw Messages API request/response shape are all confined to that one file. A
future OpenAI/Gemini/local-model adapter implements the same `Model` ABC in its own equally-isolated
file, without touching the turn/event/WorkOrder model at all.

## Backend Exception Coherence Fix (Core)

Discovered while building this arc, not part of the original plan: `SameProcessBackend` let an
artifact's own raised exception propagate raw and unwrapped, while `SeparateProcessBackend` already
wrapped `subprocess.CalledProcessError` into `ExecutionError`. Two backends implementing the same
`ExecutionBackend` interface disagreeing about exception semantics is a genuine SDK-level defect —
it also meant `EXECUTION_FAILED` (`classify_outcome()`) was unreachable via `same_process` at all,
breaking the new continuation architecture's "backend unavailable and execution failure remain
distinct and recoverable" requirement (defect 6/12 in the acceptance scenarios). Fixed in
`siphonophore_core/execution.py`: both backends now wrap an artifact's failure into `ExecutionError`
(original exception preserved as `__cause__`), and both attach whatever stdout/stderr was captured
before the failure as a plain `.detail` attribute on the exception (the same "attach curated data to
the exception" pattern `broker.py` already used for `DecisionProjection`) — incidentally resolving
the same-process output-capture stage's own disclosed "partial output... discarded... nothing to
attach it to" limitation.

## Every `siphonophore_core` Change, and Why

1. **`policy.py`: `KindExecutionPolicy`.** A new, alternative `Policy` implementation — reusable,
   model-independent (routes by declared kind, not by a caller-declared consequence tier). Needed
   because typed operations have no meaningful "consequence tier." Zero change to
   `ConsequencePolicy` or `Policy`'s own ABC contract.
2. **`execution_readonly.py` (new file): `ReadFileBackend`, `ListDirectoryBackend`,
   `SearchRepositoryBackend`.** Ordinary `ExecutionBackend` implementations — the same extension
   point every other backend uses. No code execution, no subprocess, no write path.
3. **`execution.py`: exception-wrapping coherence fix** (above). No change to `Gate`, `Decision`,
   digest verification, root-refusal checks, or either backend's actual authorized behavior — only
   what type an artifact's own failure is wrapped in, and that captured output survives onto it.

Nothing else in `siphonophore_core` changed. `Intent`, `Effect`, `Decision`, `Gate`'s HMAC minting/
verification, `Authority`/`Order`/`Scope`, and every existing backend's authorized behavior are
byte-for-byte unchanged.

## Capability Truth

`ExecutionProfile` (`composition.py`) gained `allowed_kinds` (distinct from `execution_classes` —
unrelated vocabularies for a `ConsequencePolicy`-routed profile, coincident for a
`KindExecutionPolicy`-routed one). `prompts.py`'s `build_system_prompt(profile)` derives the model's
entire operation vocabulary, the `artifact_code` requirement/prohibition, and per-kind payload
descriptions from `profile.allowed_kinds` — never a hardcoded list. `examples/repl.py`'s startup
banner shows the same `allowed_kinds` to the operator. A model under `planning_profile()` is told,
truthfully, that `run_artifact`/`write_file` do not exist in this session; a model under
`portable_profile()` is told the opposite, correctly. Neither profile's prompt ever claims an
execution class is deliverable when no backend is registered for it (`_capability_prose`'s existing
"NO backend registered" disclosure, unchanged, re-verified under both profiles).

## Explicit Security Non-Guarantees

Stated plainly, not concealed for UX neatness:

- **Mediated read is not filesystem confinement in general** — `read_file`/`list_directory`/
  `search_repository` confine to a configured `root` via real, symlink-aware path resolution
  (verified: escape via absolute path, `..`, and a symlink pointing outside `root` are all
  rejected), but nothing stops `root` itself from being configured too broadly, and nothing here
  claims OS-level mandatory access control.
- **`same_process` is weak, low isolation** — arbitrary authorized code runs with the broker
  process's own privilege. It is not gVisor, not Firecracker, not a container. Output capture
  (a prior stage) and exception-wrapping (this stage) are presentation/evidence-boundary
  corrections, not isolation upgrades.
- **A WorkOrder's `capability_requirements`/`isolation_requirements`/etc. are requests, not
  authority.** Nothing in this codebase turns them into a grant. A future Constructor choosing to
  honor them, ignore them, or grant something narrower is entirely its own, unbuilt decision.
- **A suggested worker sandbox is not a granted sandbox**, and **a capability requirement is not
  authority** — restated because it is easy to conflate in prose even when the code already keeps
  them apart.
- **A WorkOrder is not execution.** Compiling one, even `status: "final"`, performs no work,
  launches no process, and grants no credential.
- **JSONL provenance is not independent kernel evidence.** It is the harness's own account of what
  it did and observed — the same self-report/ground-truth distinction `audit.py`'s Belnap
  reconciliation already draws elsewhere in this codebase, unaddressed by this arc (JSONL logging
  and `audit.py`'s reconciliation are separate, currently unconnected mechanisms).
- **Model reasoning cannot itself attest that an effect occurred.** Only `Broker.dispatch()`'s own
  mediated result (`OperationOutcome`) is treated as truth for `TurnResult`/the event log; a
  model's own claims in `"message"` are never used to construct or infer an `Effect`.

## Explicit Non-Goals of This Arc

- The Constructor itself (reading a finalized `WorkOrder` and spinning up real, differently-scoped
  workers) — not built, deliberately, per instruction.
- `fetch_url`/any network-reaching typed operation.
- MCP or any specific tool-transport protocol.
- Session resume from the durable JSONL transcript (see below).
- Moving any reference-harness workflow concept into `siphonophore_core` beyond the three narrow,
  independently-justified additions listed above.
- A polished/colored/TUI presentation layer.
- Delegation orchestration (deciding when to delegate, spinning up a second `CognitiveLoop`
  automatically) — unchanged non-goal from every prior stage.

## Session Resume — Assessed, Deferred

The durable JSONL transcript is, in principle, sufficient raw material for a future resume feature.
Implementing resume *now* was assessed and deliberately deferred: faithfully reconstructing
`CognitiveLoop.history` (in the exact role/content shape `model.complete()` expects), any held
`Authority`, and the active `ExecutionProfile` from a replayed JSONL stream is a real design
question (What happens to an in-flight, uncommitted retry sequence at the moment of a crash? Should
a resumed session re-verify that a previously-referenced `Authority` is still valid?) that deserves
its own bounded design pass rather than a rushed addition inside an already-large arc. This is a
disclosed scope cut, not an oversight — the transcript itself is complete enough that a future stage
does not need to change this stage's event schema to build resume on top of it.

## Testing Summary

- `siphonophore_core`: `KindExecutionPolicy` (3 tests), the three readonly backends (24 tests
  covering confinement, truncation, error paths), the exception-wrapping fix (2 tests), all
  existing core tests unchanged and green.
- `siphonophore_harness`: typed-operation and WorkOrder parsing (intent_parsing.py, ~25 new tests),
  `planning_profile()`/`compose_kind_profile()` (8 new tests), capability-truthful prompts (11 new
  tests), transactional history scenarios A–J plus WorkOrder-safety plus long-session coherence
  (24 tests), bounded parse-retry (5 tests), `EventLog`/JSONL including one real end-to-end
  correlation test (11 tests), model-agnosticism structural proof (11 tests).
- `examples/repl.py`: capability-truthful banner, WorkOrder rendering, CLI surface (11 new tests).
- Full portable suite (`pytest -q -m "not k8s_cluster and not linux_root_only"`): **390 passed, 43
  deselected**, zero regressions against the pre-arc baseline (286).
- `linux_root_only`/`k8s_cluster`-marked files: not executable in this sandbox (no real root, no
  cluster); confirmed to still **collect** (import/parse) cleanly after every `CognitiveLoop`
  signature change in this arc.
- No live Anthropic API access was available in this environment (`ANTHROPIC_API_KEY` unset,
  confirmed by direct check); all of the above is deterministic/offline. See the final report for
  the recommended live-Mac acceptance script.

## V1.1 Stabilization Addendum

Written after `research/factory-harness-study/` (a four-harness comparative study of Gemini CLI,
Codex CLI, Claude Code, Cursor) and a real human REPL trial reproduced several V1 defects this
addendum resolves. Grounded in firsthand source inspection and reproduction, not mechanical
adoption of the study's own recommendations — see that directory's `CROSS_HARNESS_SYNTHESIS.md`/
`SIPHONOPHORE_RECOMMENDATIONS.md`/`SIPHONOPHORE_CURRENT_STATE_CRITIQUE.md` for the evidence this
addendum resolves against.

**1. Turn termination is no longer conflated with a resource bound.** V1's
`max_operations_per_turn=4` was simultaneously the loop's only resource control AND its de facto
logical-completion trigger, which ended real multi-observation turns mid-reasoning. The real
completion signal (a completion naming no further `"operation"`, or a compiled `work_order`) is
unchanged and is now the ONLY way an ordinary turn ends. Two independently-scoped anomaly
backstops exist instead, neither the primary termination path: `max_operations_per_turn_safety_net`
(default 50 — deliberately generous, a genuine-runaway ceiling, `TurnResult.exhausted`) and
`repeated_operation_limit` (default 3 — N consecutive requests naming the exact same `(kind,
consequence, payload, artifact_code)` is a mechanical retry loop, distinct in kind from many
different legitimate observations, `TurnResult.loop_detected`). See `siphonophore_harness/loop.py`.

**2. `SearchRepositoryBackend` re-confines every recursively-discovered path, not just the
caller-supplied top-level one.** A real, reproduced defect: a symlink (to a file or a directory)
placed inside the confined `root` and resolving outside it was read (its content passed through
`re.search`) before any confinement check ran against it — the check only ever ran when formatting
a later match, and crashed with an unhandled `ValueError` after the read had already happened.
Fixed by `_iter_confined_files()`, which independently re-resolves and re-checks confinement on
every entry the walk itself discovers, before it is ever descended into or read. See
`siphonophore_core/execution_readonly.py` and `tests/test_execution_readonly.py`'s symlink-escape
tests.

**3. `search_repository` now excludes noise, before truncation, not after.** A small, hardcoded,
unconditional exclude list (`.git`, `.venv`/`venv`, `__pycache__`, `.pytest_cache`, `node_modules`,
`*.egg-info`, `dist`, `build`, ...) plus a small, dependency-free, honestly-scoped subset of
root-level `.gitignore` matching (`_GitignoreRules` — comments/blank lines, negation, directory-only
markers, root-anchored and basename-glob patterns via `fnmatch`; explicitly NOT full git semantics —
no nested per-directory `.gitignore`, no exact `**`-vs-`*` distinction) are applied while walking,
so the existing `max_matches`/`_MAX_FILES_WALKED` bounds are only ever consumed by genuinely
relevant content — directly fixing the reproduced defect where `.git`/`.venv` sorted ahead of real
source and consumed the match budget. `ReadFileBackend`/`ListDirectoryBackend` are unchanged
(single-target/non-recursive; the noise problem is specific to the recursive fan-out search).

**4. The model-facing "consequence" field and capability prose are conditional on the active
`Policy` shape.** `ExecutionProfile.consequence_is_load_bearing` (new field, `composition.py`)
states whether `intent.consequence` genuinely selects the execution class (`ConsequencePolicy` —
`portable_profile()`) or is structurally inert (`KindExecutionPolicy` — `planning_profile()`).
`prompts.py`'s envelope-schema description of the `"consequence"` field, and its capability-prose
mapping description (previously always labeled "consequence-to-execution-class" even when the
mapping's keys were actually `Intent.kind` values, not consequence tiers), now branch on this flag.
Fixes a reproduced defect: under `planning_profile()`, the model was told its declared
`"consequence"` was a meaningful, honestly-self-assessed risk signal with real weight, when
`KindExecutionPolicy.evaluate()` never reads that field at all.

**Explicitly out of scope for this addendum** (deferred, not rejected): long-session context
compaction, a semantic outline/`ResponsePlan` mechanism, `fetch_url`, Constructor/worker
orchestration, expanding the planning profile toward a build agent. See the final tranche report
for the exact deferred-decision list and the Mac human-test procedure.

## V1.1 Stabilization Addendum, Part 2

Written after a real, live-provider REPL trial against the actual Anthropic API (`claude-sonnet-5`,
the `planning` profile, this repository's own root) reproduced two further defects the first
addendum's fixes did not cover, and surfaced two ergonomics gaps worth closing while already in this
code. Grounded in two live runs' own rendered output and JSONL transcripts, not speculation — both
runs are reproducible with `examples/repl.py --model claude-sonnet-5 --root . --verbose` given a real
API key.

**5. The bounded malformed-completion retry counter is now CONSECUTIVE, not cumulative, across one
turn.** A live run's own JSONL transcript reproduced the defect directly: three ISOLATED empty-text
completions (`block_types=['thinking','text']`, `retained_text_length=0` — the same empty-block
shape the first addendum's diagnostics fields were added to detect) occurred, each individually
recovered via the bounded retry, interspersed among 8 real, successful `Broker.dispatch()` calls, in
one 12-cycle turn that ultimately completed normally (`turn.completed operation_count=8,
exhausted=False, loop_detected=False`, no `turn.failed` at all). Under the PRIOR cumulative counter
(`parse_retries_used`, never reset), this exact real sequence would have raised on the third isolated
failure — the count would have reached `DEFAULT_MAX_PARSE_RETRIES_PER_TURN` (2) even though each
failure individually self-corrected — ending an otherwise entirely healthy turn as though its whole
input had been rejected, which is precisely the shape of defect a second live run (12 real
operations, one isolated empty completion, likewise recovered and completed normally) also avoided
under the fix. `CognitiveLoop.step()` (`loop.py`) now resets its consecutive-failure counter to zero
the moment any completion this turn parses successfully, whatever it contains — so an isolated,
transient hiccup anywhere in a long turn costs exactly one retry and is then forgotten, while N
GENUINELY CONSECUTIVE malformed completions (a real, repeated self-correction failure, not sporadic
flakiness) still exhaust the same bound and still fail the turn exactly as before
(`test_consecutive_parse_failures_still_fail_the_turn_even_after_a_real_operation`,
`test_harness_transactional_history.py`).

**6. `CognitiveLoop.last_operations` gives a caller a truthful account of a turn that ultimately
raised.** Even with fix 5 above, a turn CAN still fail after real operations occurred (a genuine
repeated parse failure, a model-transport exception, an unclassifiable outcome, an
`INTEGRITY_REJECTED` fail-closed refusal) — `step()` correctly still raises in every one of those
cases (unchanged; `self.history` was always transactionally truthful about this, per the module's
pre-existing scenarios G/H/I). What was missing was a way for a PRESENTATION layer to see that truth
without diffing `self.history` before and after a failed call. `last_operations` (reset to `()` at
the start of every `step()`, updated after every real dispatch attempt this turn) fixes this;
`examples/repl.py`'s exception handler now passes it to `render_outcome_error()`, which renders
every already-happened operation, truthfully labeled, AHEAD of the error itself, whenever
non-empty — so the terminal no longer reads as "your input was rejected" when in fact several real,
mediated effects already occurred earlier in the same failed turn.

**7. `SearchRepositoryBackend` accepts payload `path` naming a single file, not only a directory.**
A live run's model tried this against a file it had already found via a directory search, got the
prior `ExecutionError("no such directory to search: ...")`, and recovered by falling back to a
directory-scoped search — functioning, but a real, reproduced rough edge. `path` now may name either
a directory (recursive search, unchanged) or a single file (searched by itself, via the same
per-file scan logic — `_scan_file_for_matches()` — a directory walk already applies to every file it
visits); a `path` naming neither fails closed exactly as before. This is a genuine ergonomics
widening, not a confinement or bound relaxation: a single-file search is still root-confined, still
read-only, still bounded at `max_matches`/`_MAX_MATCH_LINE_CHARS`, and touches exactly the one file
named.

**8. `search_repository`'s recursive walk excludes a harness's own generated session-log
directory by default.** A prior real trial's `search_repository` fanned into
`siphonophore-sessions/*.jsonl` (this reference REPL's own durable JSONL transcripts,
`session_log.py`) and surfaced a stale session's content as if it were ordinary repository/source
evidence to the CURRENT investigation. `SearchRepositoryBackend` (`execution_readonly.py`) gained a
constructor-supplied `extra_excluded_dir_names` (empty by default — this core module stays
harness-neutral and adds no hardcoded, project-specific name to its own noise list, unlike the
already-hardcoded, genuinely universal `.git`/`.venv`/`__pycache__`/etc. set);
`composition.py`'s `planning_profile()` supplies `session_log.DEFAULT_SESSION_LOG_DIR_NAME` (a new
shared constant, so `examples/repl.py`'s own default log path and this exclusion cannot drift out of
sync) as that exclusion. `read_file`/`list_directory` are unaffected — a caller who explicitly wants
to inspect a session log (as generic JSONL provenance evidence, per this document's own "JSONL
provenance is not independent kernel evidence" non-guarantee) can still name it directly; only the
noise-reducing recursive default excludes it.

**Live-provider acceptance.** Two fresh sessions, both against the real Anthropic API
(`claude-sonnet-5`), both driving the exact primary scenario ("inspect the repository and explain how
the planning profile prevents modification, without compiling a WorkOrder"): run 1 dispatched 8 real
operations (2 `list_directory`, 3 `read_file`, 2 `search_repository`, plus a `read_file` repeat) and
recovered 3 isolated empty completions; run 2 dispatched 12 real operations and recovered 1. Both
completed in exactly one logical turn (`turn_id` constant across every cycle), named no `work_order`
(as instructed), produced zero `turn.failed` events, and every dispatched `intent_id` was distinct
across the whole turn (no duplicate dispatch from retry handling, in either run).

**Explicitly out of scope for this addendum, Part 2** (deferred, not rejected, unchanged from Part
1's list): long-session context compaction, a semantic outline/`ResponsePlan` mechanism,
`fetch_url`, Constructor/worker orchestration, expanding the planning profile toward a build agent.

## Post-Review Hardening Addendum (bounded tranche over `311f498..76c3dc3`)

A fresh review of the V1.1 work reported ten findings (3 HIGH, 5 MEDIUM, 2 LOWER). Each was treated
as a hypothesis, not established truth: inspected, reproduced with a focused regression test where
feasible, characterized, then minimally fixed. Nine reproduced as real defects (fixed); one (#6,
below) did not reproduce as a behavior defect — the runtime was already correct, only its own
docstring's prose was imprecise, and that prose is fixed instead. Branch:
`hardening/reference-harness-v1.1-postreview`.

**HIGH**

1. **`CognitiveLoop.last_operations` missed the operation attempted on the fail-closed
   `INTEGRITY_REJECTED` path.** `step()` builds and appends the `OperationOutcome` (and updates
   `self.last_operations`) only on the success/recoverable branch, never on the
   `not dispatched_ok and category in _FAIL_CLOSED_CATEGORIES` raise path — even though `commit()`
   had already made that same attempt permanent in `self.history` moments earlier. Reproduced with a
   broker stand-in that dispatches once for real, then raises a forged-decision
   `DecisionVerificationError` on the next call: `last_operations` held only the first operation,
   not the second (the one that actually raised), directly contradicting this module's own "updated
   after every real dispatch attempt, successful or not" claim. Fixed by building and appending the
   `OperationOutcome` immediately before the `raise`, exactly as every other branch already does.
   Regression: `test_last_operations_includes_the_operation_attempted_on_the_fail_closed_integrity_path`
   (`test_harness_transactional_history.py`).

2. **`SearchRepositoryBackend`'s directory walk fully materialized and sorted before applying its
   file-count bound, and had no cycle protection.** Both reproduced. `sorted(_iter_confined_files(...))`
   pulled the ENTIRE confined, walkable tree into memory and sorted it before `_MAX_FILES_WALKED`
   ever applied — measured ~1.9s to walk 60,000 files against a 5,000-file bound, cost scaling with
   total tree size, not the bound. Directory symlinks ARE followed (`is_dir()` is true for a
   symlink-to-directory, and the symlink path itself, not its resolved target, is re-queued for
   descent) — a self-referential symlink happened not to hang only because the OS's own per-lookup
   ELOOP limit incidentally terminated the ever-lengthening resolved path after a few dozen
   iterations; a genuine two-node cyclic topology (A/link_to_b -> B, B/link_to_a -> A) does NOT
   merely hang slower, it silently returns duplicated/amplified results — reproduced directly: the
   same real file's matches appeared 20 times instead of once, before this fix (confirmed against
   the pre-fix code; the self-referential single-node case alone did not amplify, since it produced
   no matches to duplicate). Fixed with two independent changes: `_iter_confined_files` now tracks
   the resolved real identity of every directory already queued for descent and skips a re-visit
   (deterministic cycle termination, not dependent on any OS limit); `SearchRepositoryBackend.run()`
   now bounds how many entries it ever pulls from the (lazy) walk via `itertools.islice`, sorting
   only that bounded set, rather than materializing the whole tree first (large-tree cost now
   ~0.3s regardless of total tree size beyond the bound). Path confinement itself is unchanged.
   Regressions: `test_search_repository_self_referential_directory_symlink_terminates`,
   `test_search_repository_two_node_cyclic_directory_symlinks_terminates`,
   `test_search_repository_symlinked_directory_resolving_inside_root_is_searched_once`,
   `test_search_repository_large_tree_beyond_file_count_bound_is_truncated_without_full_materialization`
   (`test_execution_readonly.py`).

3. **A non-string payload value (e.g. a JSON array for `path`) raised a raw `TypeError`, escaping
   `CognitiveLoop`'s dispatch-handling boundary.** Reproduced directly: `ReadFileBackend`,
   `ListDirectoryBackend`, and `SearchRepositoryBackend` all pass `intent.payload["path"]` straight
   into `Path(...)`/`_confine()`; a truthy non-string value (list/dict/number) skips the existing
   `if not path` empty-check and reaches `Path()`, raising `TypeError` — a type `step()`'s
   `except (GateViolation, ExecutionError, IdentityError)` does not catch, so it propagates out of
   the loop entirely instead of failing closed. `SearchRepositoryBackend.pattern` had the identical
   gap. A related gap one layer up: `parse_intent()` never checked that `payload` itself decodes to
   a JSON object, so a non-dict `payload` (e.g. a JSON array) produced an `Intent` whose `.payload`
   isn't a dict, and the first backend calling `.get()` on it would raise a raw `AttributeError`.
   Fixed at both layers: `_confine()` (`execution_readonly.py`) now rejects a non-string path with
   `ExecutionError`, `SearchRepositoryBackend.run()` now requires `pattern` to be a non-empty string
   the same way, and `parse_intent()` (`intent_parsing.py`) now requires `payload` to decode to a
   JSON object, failing closed with a recoverable `IntentParseError` (self-correctable within the
   existing bounded parse-retry policy) rather than an unhandled exception at the backend boundary.
   Regressions: `test_read_file_non_string_path_fails_closed_not_a_raw_type_error`,
   `test_list_directory_non_string_path_fails_closed_not_a_raw_type_error`,
   `test_search_repository_non_string_pattern_fails_closed_not_a_raw_type_error`,
   `test_search_repository_non_string_path_fails_closed_not_a_raw_type_error`
   (`test_execution_readonly.py`); `test_parse_intent_rejects_non_object_payload`
   (`test_harness_intent_parsing.py`).

**MEDIUM**

4. **Loop detection compared only consecutive requests, so an alternating probing pattern evaded
   it until the 50-operation safety net.** Reproduced directly: a scripted `A, B, A, B, A, B`
   sequence (repeated_operation_limit=3) ran the `ScriptedModel` dry (6 completions consumed, no
   loop ever detected) under the pre-fix consecutive-only comparison. Fixed with a small, BOUNDED
   sliding window (`2N-1` most recent operation-request signatures, N=`repeated_operation_limit`) —
   sized so an alternating period-2 pattern trips at the same Nth occurrence a purely consecutive
   repeat already did — built fresh at the start of every `step()` call (never persists across
   turns) and evicted (`collections.deque(maxlen=...)`) as it grows, so a legitimate revisit of an
   earlier observation much later in a long, otherwise-varied turn ages out of the window and is
   never penalized; this is deliberately NOT a history-wide duplicate ban.
   Regressions: `test_alternating_operations_trip_the_loop_detector`,
   `test_a_distant_revisit_in_a_long_varied_turn_does_not_trip_the_detector` (`test_harness_loop.py`).

5. **`artifact_code` forbidden-field checking used truthiness, letting an empty string through.**
   Reproduced directly: `operation.get("artifact_code")` on the FORBIDDEN (non-code-bearing-kind)
   side is falsy for `""`, so `{"kind": "read_file", "artifact_code": ""}` silently passed where any
   non-empty value would have been rejected. Fixed by checking `is not None` on that side instead
   (the REQUIRED side, for code-bearing kinds, is unchanged and correctly still uses truthiness --
   an empty string is not a usable artifact there either way).
   Regression: `test_parse_intent_typed_operation_rejects_empty_string_artifact_code`
   (`test_harness_intent_parsing.py`).

6. **Architecture question, resolved: operations + a terminal WorkOrder in one turn is intentional,
   not a contract violation.** `TurnResult`'s own docstring read as claiming `operations` non-empty
   and `work_order` non-None are mutually exclusive for a whole turn. What `parse_turn()` actually
   enforces is narrower and per-COMPLETION: a single completion may not name both `"operation"` and
   `"work_order"`. A multi-cycle turn dispatching several real, mediated read-only operations in
   earlier cycles and THEN compiling a WorkOrder in a later cycle of that same turn already worked
   correctly at runtime (unchanged) and is the intended shape for a read-only planning agent that
   cannot itself execute the work it specifies ("investigate, then specify"). This did not reproduce
   as a behavior defect; the docstring's prose was the actual inaccuracy, and is corrected
   (`outcome.py`) to state the per-completion (not per-turn) scope of the exclusivity. Confirming
   regression (not a bug-fix test): `test_operations_then_a_work_order_in_the_same_turn_is_valid_not_mutually_exclusive`
   (`test_harness_transactional_history.py`).

7. **Loop-detector identity always included `consequence`, even when it is structurally inert.**
   `KindExecutionPolicy`-routed profiles (`compose_kind_profile()`, `planning_profile()`) never read
   `intent.consequence` at all (`ExecutionProfile.consequence_is_load_bearing = False`) — two
   otherwise byte-identical requests differing only in declared consequence produce the identical
   effect under such a profile, yet the pre-fix signature still treated them as distinct, letting a
   model evade detection by cycling a field that changes nothing. Fixed by threading
   `consequence_is_load_bearing` (new `CognitiveLoop` constructor parameter, defaulting to `True` --
   unchanged behavior for every existing caller; `examples/repl.py` now passes
   `profile.consequence_is_load_bearing`) into the signature: `consequence` is folded in only when
   it is actually load-bearing for the active profile. `test_harness_structural_proof.py`'s
   constructor-shape assertion is extended to allow this one additional plain, inert `bool` --
   the same category (never read from or influenced by the model's own completion, not a capability)
   its own docstring already established for the existing integer/callable parameters.
   Regressions: `test_consequence_field_is_load_bearing_by_default_pairwise_distinct_requests_never_trip`,
   `test_consequence_field_excluded_from_loop_signature_when_not_load_bearing` (`test_harness_loop.py`).

8. **Session-log field bounding applied only to top-level strings, not nested structured data.**
   Reproduced directly: `_redact_and_bound_event` checked `isinstance(value, str)` on each
   TOP-LEVEL event field only — `operation.result`'s own `detail` (a whole `Effect.detail` dict,
   e.g. `ReadFileBackend`'s `{"content": "...", ...}`) is a dict, so a large captured string nested
   inside it (bounded only by the much larger, intentionally-larger backend-capture bound, up to
   200,000 characters) reached a JSONL log line entirely unbounded by this module's own,
   intentionally smaller per-field bound (10,000 characters in the reproduction, unbounded either
   way pre-fix). Fixed with `_bound_value()`, applied recursively through dicts and lists, leaving
   the existing `work_order` whole-payload exemption unchanged.
   Regressions: `test_nested_string_fields_inside_a_dict_are_also_bounded`,
   `test_nested_string_fields_inside_a_list_are_also_bounded` (`test_harness_session_log.py`).

**LOWER (deliberately not addressed in this tranche)**

9. `EventLog.emit()` opens/closes its log file on every call — a pure efficiency concern (no
   correctness or security impact; every event is still durably, atomically appended). Holding a
   file handle across the object's lifetime would need its own flush/close discipline and is a
   design change, not a bounded fix — left for a future tranche.

10. Containment logic is duplicated between `_confine()` and `_iter_confined_files()` — a DRY
    concern, not a defect (both independently enforce the identical resolve-then-check invariant,
    and finding #2's fix touches `_iter_confined_files()` directly; consolidating them now, in a
    security-sensitive path, was judged more likely to introduce risk than to reduce it within this
    tranche's bounded scope). Left for a future tranche.

**Live-provider acceptance.** Two fresh sessions against the real Anthropic API (`claude-sonnet-5`),
both driving the established primary scenario ("inspect the repository and explain how the planning
profile prevents modification, without compiling a work order"): run 1 dispatched 14 real operations
(1 `list_directory`, 4 `read_file`, 9 `search_repository`) recovering 5 isolated malformed
completions; run 2 dispatched 17 real operations (1 `list_directory`, 5 `read_file`, 11
`search_repository`) recovering 12. Both completed in exactly one logical turn, every dispatched
operation classified `executed`, zero `turn.failed` events, no exhaustion, no loop falsely detected
despite run 2's many `search_repository` calls exercising the finding-#2 fix directly against this
repository's own real tree. A third session asked the model to investigate this loop detector's own
implementation and then compile a final WorkOrder describing a follow-up task: it dispatched 19 real
operations (17 `search_repository`, 2 `read_file`, all `executed`, loop never falsely detected
despite 17 consecutive `search_repository` calls in the same turn) and then finalized a WorkOrder in
that SAME turn/turn_id -- `TurnResult.operations` non-empty and `.work_order` non-None together,
live confirmation of finding #6's resolved architecture question against the real API, not just the
deterministic regression test. `INTEGRITY_REJECTED` (finding #1) was not
exercised live: it requires a forged/tampered `Decision`, structurally unreachable through a real
`Broker.dispatch()` call (per `classify_outcome()`'s own docstring) — covered instead by the
deterministic regression test above, matching this codebase's existing convention for this category
(`test_harness_outcome.py`, `_IntegrityRejectingBroker`).

**Explicitly out of scope for this tranche** (per the tranche brief, unchanged): Constructor, worker
orchestration, VM lifecycle CLI, workspace/Git transfer, session compaction, resume, `fetch_url`/
network capabilities, MCP, primary-agent redesign.
