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
