# Reference Harness V1 Horizon — Reconciliation

**Status: BOUNDED RECONCILIATION STAGE. No functional/behavioral changes accompany this document.**

Branch `reconcile/reference-harness-v1-horizon`, created from `38f38dc` (`harness: expose model
boundary diagnostics`), tip of `observe/reference-harness-model-boundary`. This document reconciles
five prior stages (`docs/REFERENCE_HARNESS_ASSESSMENT.md`, `REFERENCE_HARNESS_TARGET_DESIGN.md`,
`REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md`, `REFERENCE_HARNESS_TURN_BOUNDARY_ASSESSMENT.md`,
`REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md`) against current source, current tests, and one round
of real human (Mac) testing, and re-derives what remains between current `HEAD` and a genuinely
usable "Reference Harness V1" against the north star stated for this stage. Every claim is labeled
**SOURCE FACT** (re-verified directly against current code/tests in this stage), **INFERENCE** (a
conclusion drawn from source facts, flagged as such), or **RECOMMENDATION**. `~/research` was not
accessed at any point.

## North Star

Restated from this stage's own framing, not re-derived: Siphonophore is a platform-independent
execution-security SDK; Kubernetes is one native substrate below the execution boundary, not
Siphonophore's identity. The reference harness is only a consumer. The governing invariant:

> Ordinary model conversation should feel like ordinary model conversation. Every effect-producing
> action requested by the model must become an explicit operation and traverse Siphonophore's
> mediated authorization/execution path. The cognitive/model loop must never itself become a hidden
> effect path. Conversation that requests no effect must not manufacture execution. When an
> operation executes, its result must cross a structural result boundary and be usable by the model
> to finish the original user turn.

Shorthand: **conversation is not execution, but every real effect is mediated.**

## Verified Current State

**SOURCE FACT.** Starting checkout: branch `observe/reference-harness-model-boundary`, `HEAD =
38f38dca2b49f9d2c55a7fe78e93c64cc6477b43`, working tree clean (`git status --porcelain` empty).
This is exactly the observability state named in prior-session memory — no discrepancy found, so
the branch was created as instructed rather than reporting a mismatch.

**SOURCE FACT.** Full portable suite, re-run this stage (`.venv`, `pytest -q -m "not k8s_cluster
and not linux_root_only"`): **272 passed, 43 deselected.** Consistent with the commit history's own
recorded counts (224 baseline → 250 after `61fbdcf` → 272 after `38f38dc`'s added diagnostics
tests).

Every item the task's own "CURRENT STATE THAT APPEARS TO HAVE ALREADY LANDED" list named was
independently re-verified against current source in this stage, not accepted from memory or from
commit messages:

| Claimed-landed item | Verified? | Where |
|---|---|---|
| Response envelope with optional `operation` | **Yes** | `intent_parsing.py:66-96` (`parse_turn`), `ParsedTurn.intent: Intent \| None` |
| Message-only result with no execution attribution | **Yes** | `outcome.py:88-101` (`MessageOnlyResult`) — no `intent_id`/`execution_class`/`decision`/`detail` field exists on the type at all, by omission |
| Conversation-only path that never constructs `Intent` | **Yes** | `intent_parsing.py:91-92` returns before `parse_intent()` is ever called when `operation` is absent/null |
| Early harness validation of `artifact_code` | **Yes** | `intent_parsing.py:122-123`, required/non-empty inside `operation`, raised as `IntentParseError` before an `Intent` exists |
| Profile-derived model capability description | **Yes** | `prompts.py:build_system_prompt(profile)`, consumed at `repl.py:285` |
| Structured `DispatchResult`/`DecisionProjection` | **Yes** | `broker.py:53-64`, `outcome.py:37-86` |
| Outcome categories | **Yes** | `outcome.py:104-162`, closed 8-value enum, `classify_outcome()` |
| Compact execution trace | **Yes** | `repl.py:147-159` (`render_turn_result`) |
| Terminal readline/history | **Yes** | `repl.py:39-43` |
| Answer-first presentation | **Yes** | `repl.py:191-193` (`render_turn`: message first, trace footer after) |
| Suppression of empty `detail={}` | **Yes** | `repl.py:157-158` (`if result.detail:`) |
| Startup clear/banner | **Yes** | `repl.py:79-131` |
| Model-boundary diagnostics | **Yes** | `model_anthropic.py:52-110` (`ModelResponseDiagnostics`, `last_diagnostics`), `loop.py:88-90` |
| Retained raw completion visibility in verbose mode | **Yes** | `loop.py:69-77,84-90`, `repl.py:196-198,234-238` |

**INFERENCE.** None of this list needs to be redesigned or regressed. The prior five documents'
own analysis was not merely trusted — it was independently re-derived against current
`intent_parsing.py`, `loop.py`, `broker.py`, `outcome.py`, `composition.py`, `execution.py`,
`model_anthropic.py`, and `examples/repl.py` in full, in this stage, and found accurate in every
particular checked.

**Also verified, beyond the explicit landed-list, and materially relevant:** the substrate-
selection and authority-exposure work the older `REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md` called
Stage 3/4/5 is **substantially already done**, via a different concrete path than that plan
specified (composition + prompt work landed inside the turn-contract commits, not as separate
staged commits):

- `siphonophore_harness/composition.py` — `compose_profile()`/`portable_profile()`/
  `ExecutionProfile`, exactly the "named wiring + discoverability" convenience Stage 3 called for.
- `examples/repl.py:242-255,288-289` — a `--grant-root-authority` flag and
  `render_authority_banner()`, satisfying Stage 5's "documented advanced path" minimum bar (session
  can hold a pre-granted `Authority`; authority context is visible; no orchestration was added).
- **Not done**: a CLI flag to select a *different* profile than `"portable"` (`repl.py:284` hardcodes
  `portable_profile()`). `compose_profile()` itself is substrate-neutral and already sufficient for
  this — nothing blocks it structurally; it simply hasn't been wired to a flag. This is a small,
  non-blocking gap, addressed in **Later Work Sequence** below.

## Evidence From Human Trials

Re-examined directly against current source, not re-asserted from the trial report:

1. **`SameProcessBackend` artifact stdout escapes directly into the operator terminal.**
   **SOURCE FACT, still present, unchanged by any commit through `38f38dc`.**
   `siphonophore_core/execution.py:104-111`: `exec(intent.artifact_code, namespace)` runs with no
   `contextlib.redirect_stdout`/`redirect_stderr`, no size bound, no capture of any kind;
   `Effect.detail` is hardcoded to `{}` regardless of what the executed code printed. Contrast
   `SeparateProcessBackend` (`execution.py:126-141`), which already does capture stdout into
   `Effect.detail["stdout"]` via `subprocess.run(capture_output=True)`. This asymmetry between the
   two default-registered backends is real and was already named, not fixed, by
   `REFERENCE_HARNESS_TURN_BOUNDARY_ASSESSMENT.md` §12/§19 and `REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md`
   §21 item 1 ("explicitly deferred"). Classified there, and reconfirmed here: this is a
   **presentation/capture boundary the codebase never built for this backend**, not a mediation
   bypass — the code that runs is exactly what the Decision authorized (digest-checked); what
   escapes is *evidence of what happened*, not the effect's authorization.

2. **The execution result was not fed back to the model to finish the original turn.**
   **SOURCE FACT, still present, unchanged by any commit through `38f38dc`.**
   `siphonophore_harness/loop.py:46-100` (`CognitiveLoop.step()`): one call to `model.complete()`,
   one `parse_turn()`, at most one `Broker.dispatch()`, then `step()` returns unconditionally.
   Nothing in `step()`, `broker.py`, or `repl.py`'s `main()` loop (`repl.py:302-326`) ever calls
   `model.complete()` a second time within the same user turn using the `Effect`/outcome just
   produced. The Effect is appended to `history` (`loop.py:99`) purely for *future* turns' context —
   the *current* turn ends at the REPL prompt without the model ever seeing its own operation's
   result. This is the turn-contract work's one genuinely unclosed architectural gap relative to
   this stage's north star (see **Architectural Gaps** below) — not a regression, since no prior
   design document claimed to close it (`REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md` §21 scopes
   itself to the message/operation envelope only, explicitly not to iteration).

3. **`same_process` inherits the broker's ambient filesystem/environment capabilities.**
   **SOURCE FACT, and correctly-scoped, not a defect.** `execution.py:104-111`, unchanged: `exec()`
   runs in-process with the broker's own full privilege — this is `same_process`'s documented
   identity as the lowest-isolation tier (`DESIGN.md` §2, re-confirmed by the class's own
   docstring, `execution.py:88-89`), not an unenforced instruction the model failed to respect. No
   commit through `38f38dc` claims otherwise anywhere in `prompts.py`'s model-facing text (checked
   directly, §**Verified Current State** above) or in `repl.py`'s banner. **The one thing worth
   tightening, not urgently:** nothing in the current model-facing prompt or operator banner states
   the *absence* of a filesystem/network boundary for `same_process` as a guarantee an operator
   should not assume — see **What `same_process` Should/Should Not Claim** below.

4. **A previous raw-HTML incident is consistent with the stdout escape reproduced here.** Same
   mechanism as item 1 — no separate defect.

5. **Non-JSON model output can still become `input_rejected`.** **SOURCE FACT, still true, and by
   design at this stage.** `intent_parsing.py:78-81` still raises `IntentParseError` with no
   retry/repair; `CognitiveLoop.step()` has no retry loop (`loop.py`, confirmed). What changed
   through `38f38dc` is diagnostic-only: `ModelResponseDiagnostics` (`model_anthropic.py:52-68`)
   and `last_completion`/`last_diagnostics` on `CognitiveLoop` (`loop.py:43-44,88-90`) now survive a
   parse failure so an operator running `--verbose` can see *why* a completion failed to parse
   (non-JSON prose, empty response, non-text blocks) without guessing. This is exactly what the
   commit message for `38f38dc` states its own purpose to be: enabling empirical study *before*
   implementing repair/retry, not a repair/retry implementation itself. Confirmed accurate.

## Architectural Gaps

Ranked by how directly each blocks the north star, not by discovery order.

### Gap 1 — No iterative turn / model continuation (the central gap)

**INFERENCE**, grounded in the source facts above. The turn-contract work (`61fbdcf`) correctly
separated *conversation* from *execution* at the level of one dispatch: a turn either requests
zero operations (message-only, no `Broker.dispatch()` call) or exactly one (dispatched, mediated,
attributed). It did not implement the north star's further requirement — that **a user turn may
resolve through zero or more mediated operation/result cycles before a final model response** — and
no prior document claims it did; `REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md` §21 explicitly scopes
itself to the envelope shape only. Acceptance scenario 3 ("inspect this URL and tell me what the
project does... model answers the original question without requiring the user to ask 'What did
you find?'") is **not met by current `HEAD`** — reproduced directly by Mac-trial evidence item 2,
and confirmed structurally by reading `loop.py`/`repl.py` in full: there is no code path in this
repository, today, that calls `model.complete()` a second time using an `Effect` as input.

### Gap 2 — Output/result boundary for `same_process` is not built

Restated from Evidence item 1. **INFERENCE, not previously stated this way in prior documents:**
Gap 1 and Gap 2 are not independent — **Gap 2 is a hard prerequisite for Gap 1 to be meaningful**,
not merely a "separate defect co-occurring with a common missing boundary" as the task's framing
speculates. Even if iterative continuation (Gap 1) were implemented today, feeding
`SameProcessBackend`'s `Effect` back to the model would feed it `detail={}` — structurally empty,
regardless of what the artifact actually printed or fetched. Acceptance scenario 3 specifically
requires the model to answer using the *content* the operation retrieved; under the current
`same_process` backend, that content exists only as unstructured terminal output, never as
something the model's next completion could read. **Closing Gap 1 without first closing Gap 2 would
produce a hollow continuation** — the model would receive an empty result and could only fabricate
or decline to answer, which is arguably worse than today's honest silence. This reordering — Gap 2
before Gap 1 — is this document's one material correction to the ordering the task-framing
proposed ("iterative continuation" listed ahead of "output normalization").

### Gap 3 — Non-JSON completion has no repair/retry

Restated from Evidence item 5. Independent of Gap 1/2 in mechanism, but related in shape: a bounded
retry-on-parse-failure is structurally a *degenerate case* of a bounded multi-step turn (the model
gets another chance to produce a valid completion within the same user turn, without a real
operation in between). **RECOMMENDATION**: do not design retry/repair in isolation from the
iterative-turn design in Gap 1 — the same "how many bounded internal steps does one user turn get"
question governs both, and solving them with two different bounding mechanisms would be needless
duplication.

### Gap 4 — Substrate-profile selection has no operator-facing flag

Restated from **Verified Current State**. `compose_profile()`/`ExecutionProfile` already provide
everything needed; `repl.py:284` simply never calls anything but `portable_profile()`. Narrow,
mechanical, no open design question. Not on the critical path to the north star (the north star is
about the conversation/execution/result boundary, not about which substrate a REPL session
targets), so it is ranked below Gaps 1-3.

### Gap 5 — `README.md`'s "Not yet implemented" bullet on the reference harness is stale

**SOURCE FACT.** `README.md:255-260` still reads: "...surfaces the resulting `Effect` but never the
`Decision` behind it — `Broker.dispatch()` returns only the `Effect`..." This was true before
`REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md` Stage 2 landed and is **false today** —
`Broker.dispatch()` returns a `DispatchResult` carrying a `DecisionProjection`
(`broker.py:53-64`, `outcome.py:61-86`). This was already flagged as future, separate,
non-blocking work by that plan's own §11 open question 3 ("out of scope for every stage in this
plan") — restated here as still open, not newly discovered, and **not** addressed by this stage
(documentation-only correction, no design content, appropriately left for whichever future stage
actually touches `README.md`).

## Decisions / Superseded Decisions

| Prior decision | Status | Basis |
|---|---|---|
| Option C (response envelope with optional operation), `REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md` §1 | **STANDS, fully implemented** | Verified in **Verified Current State** above |
| Three narrow `GateViolation` subclasses (`PolicyDeniedError`, `DecisionVerificationError`, `NoBackendRegisteredError`), `IMPLEMENTATION_PLAN.md` Stage 1 | **STANDS, implemented** | `execution.py:53-77` |
| `Broker.dispatch()` widened via Option E (curated `DecisionProjection`, not raw `Decision`), `TARGET_DESIGN.md` §10 | **STANDS, implemented** | `broker.py`, `outcome.py` |
| "Named wiring" composition convenience should exist in `siphonophore_harness/`, `TARGET_DESIGN.md` §16 open Q5 | **STANDS, implemented** (as `composition.py`) | — |
| `README.md`'s description of `Broker.dispatch()` returning only `Effect` | **SUPERSEDED / STALE** | Gap 5 above — a documentation-only staleness, not a design reversal |
| `IMPLEMENTATION_PLAN.md`'s stage numbering/sequence (Stage 1→2→3→4→5→6→7) | **SUPERSEDED as a literal roadmap** — the actual commit sequence (`61fbdcf`, `38f38dc`) delivered materially the same requirements through a different, coarser staging (envelope + composition + prompt work bundled together rather than kept as five separate stages) | The requirements it named (§16 acceptance criteria) are almost entirely satisfied; the specific stage *boundaries* it proposed were not followed literally, and did not need to be |
| `REFERENCE_HARNESS_TURN_BOUNDARY_ASSESSMENT.md` §20 Option A/D risk analysis (pulling harness/model-adapter concerns into core) | **STANDS** — no core file changed for the envelope work (§17 of `TURN_CONTRACT_DESIGN.md`, re-confirmed: `siphonophore_core/` untouched by `61fbdcf`/`38f38dc`) | `git show --stat` for both commits, this stage |
| "same_process stdout capture is deferred," `TURN_CONTRACT_DESIGN.md` §21 item 1 | **STANDS as a decision to defer, but the deferral's own bound has now been reached** — see Gap 2's promotion above | This stage's own reasoning (Gap 2) |

No design decision found in any prior document **conflicts** with the north star as newly stated
for this stage. The turn-contract design's Option C is, if anything, a closer match to the north
star's "response envelope: message + optional operation" framing than this stage's own prompt
anticipated finding — it is effectively already implemented, not merely compatible.

## V1 Acceptance Properties

Restated from the five reference scenarios, checked against current `HEAD`:

| Scenario | Current behavior | Met? |
|---|---|---|
| 1. "Good Morning" | `{"message": "..."}`, no `operation`; `Broker.dispatch()` never called; REPL shows message only, no trace footer | **YES** |
| 2. "What is the capital of Japan?" | Same as above | **YES** |
| 3. "Inspect this URL and tell me what the project does" | Model can emit a real `operation`; it dispatches, mediates, produces an `Effect` — but the turn ends there. For `same_process`, `Effect.detail={}` regardless of what was fetched (Gap 2); even with a hypothetical fix to Gap 2, the model is never given the result to answer with (Gap 1). The user must ask a follow-up ("What did you find?"), which — per Mac-trial evidence item 3 above — currently risks `input_rejected` if the model's follow-up isn't valid JSON | **NO** — this is precisely the acceptance test Gaps 1+2 exist to close |
| 4. "Use Python to calculate..." | `artifact_code` legitimately present; dispatches through `same_process`/`separate_process` exactly as designed | **YES**, and correctly not generalized — nothing in current code treats this as evidence that all future operations must be Python (see **Non-Goals**) |
| 5. Policy denial distinguishable from malformed/unavailable/failed/identity/integrity | `OutcomeCategory`'s 8-value closed enum (`outcome.py:104-162`), rendered with distinct plain-language labels (`repl.py:104-113`) | **YES** |

**Four of five scenarios are already met by current `HEAD`.** The remaining one (scenario 3) is not
a broad, diffuse shortfall — it isolates to exactly the two gaps this document ranks first.

## Ordered Work Sequence

Re-derived from current state, not accepted from the task's own candidate ordering (which listed
"iterative continuation" ahead of "output normalization" — see Gap 2's reasoning above for why this
document reorders those two).

1. **`same_process` output capture** (Gap 2). Core-layer (`siphonophore_core/execution.py`), narrow,
   mechanical, precedented by `SeparateProcessBackend`'s existing `capture_output=True` pattern.
   No open design question: capture stdout/stderr via `contextlib.redirect_stdout`/`redirect_stderr`
   around the `exec()` call, bound the captured size (a real gap `SeparateProcessBackend` also has
   today — `execution.py`'s own docstring never addresses truncation for either backend — worth
   closing for both in the same pass, though `separate_process`'s *unbounded-size* gap is lower
   priority since it already doesn't leak to the terminal), place the result into `Effect.detail`
   exactly as `separate_process` already does. This is a **core** change (the one place in this
   entire sequence that touches `siphonophore_core/`), and it is small and independently
   justifiable regardless of whether Gap 1 is ever closed (any direct `siphonophore_core` consumer
   of `same_process` benefits, not only this reference harness).
2. **Iterative turn contract — design stage first** (Gap 1). This needs the same rigor the envelope
   contract got (`TURN_BOUNDARY_ASSESSMENT.md` → `TURN_CONTRACT_DESIGN.md` → implementation) before
   any code changes — the open questions are real: how many bounded internal operation/result
   cycles does one user turn get; whether the bound is a fixed constant, a `CognitiveLoop`
   constructor parameter, or profile-derived; how multiple `DispatchResult`/exception outcomes
   within one turn are represented back to the model (one "effect" history entry per cycle, already
   the existing mechanism, or something richer); how the REPL renders more than one trace footer
   under one user turn while keeping "answer-first" presentation intact; what happens when the
   bound is exhausted before the model produces a final message (fail closed with an honest
   "operation limit reached" turn, not a silent truncation); whether Gap 3 (non-JSON retry) is
   folded into the same bound or kept separate. **Do not implement before this design stage
   completes** — this is the one place in the whole horizon where jumping straight to code would
   repeat the exact mistake `TURN_BOUNDARY_ASSESSMENT.md` itself was written to avoid.
3. **Iterative turn contract — implementation.** Depends on 1 (a meaningful result to feed back) and
   2 (a settled bound/representation design).
4. **Non-JSON repair/retry** (Gap 3), if the design stage in (2) determines it shares the same bound
   mechanism — likely folded into (3)'s implementation rather than a separate stage.
5. **Substrate-profile selection flag** (Gap 4). Independent of 1-4; can proceed in parallel or any
   order relative to them. Small, no open design question (`compose_profile()` already sufficient).
6. **`README.md` staleness correction** (Gap 5). Documentation-only, independent, no ordering
   constraint — can happen whenever convenient, ideally bundled with whichever stage next touches
   `README.md`'s "Not yet implemented" section anyway.
7. **Stronger substrates reachable from the reference harness itself** (`uid_cgroup`/`k8s_pod`
   selectable via the flag from item 5). Explicitly **not required for V1** — the north star's own
   framing keeps Kubernetes as a substrate-layer concern already proven at the core level
   (`tests/test_harness_loop_k8s_cluster.py`), not a reference-harness UX priority.
8. **Typed operation surface** (whether `run_artifact`/`write_file`/a future `fetch_url`/`read_file`
   should be structurally distinct operations rather than "every kind executes via
   `artifact_code`"). Explicitly **not decided by this document** — the north star instructs
   against assuming `artifact_code` should become the universal representation prematurely, and
   nothing in current evidence forces this decision now. Left open for a future, dedicated design
   stage once more real usage exists to reason from.

## MUST NOW / NEXT / LATER / DEFERRED

- **MUST NOW**: nothing — this stage is reconciliation-only, no implementation.
- **NEXT** (the next bounded implementation stage after this one, see below): `same_process` output
  capture (work-sequence item 1).
- **NEXT-AFTER-THAT** (design, not implementation, but the second concrete stage in sequence):
  iterative turn contract design (work-sequence item 2).
- **LATER**: iterative turn contract implementation (3); non-JSON repair/retry (4); substrate-profile
  selection flag (5); `README.md` staleness fix (6).
- **EXPLICITLY DEFERRED**: stronger substrates reachable from the reference harness's own CLI (7);
  typed operation surface redesign (8); delegation *orchestration* (deciding when to delegate,
  spinning up a second `CognitiveLoop` automatically — unchanged non-goal from every prior
  document); a dedicated `execution_id` distinct from `intent_id`; any Kubernetes-specific concept
  entering `siphonophore_harness`'s own vocabulary; a polished/colored/TUI presentation.

## Next Bounded Implementation Stage

**`same_process` output capture.**

- **Scope**: `siphonophore_core/execution.py`, `SameProcessBackend.run()` only. Wrap the `exec()`
  call in `contextlib.redirect_stdout`/`redirect_stderr` (two `io.StringIO` buffers), populate
  `Effect.detail` with the captured text (mirroring `SeparateProcessBackend`'s existing
  `{"stdout": ..., ...}` shape so a future presentation layer can treat both uniformly — see
  `TARGET_DESIGN.md` §6/§11's already-named, still-open "shared `Effect.detail` shape" question,
  which this stage can resolve incidentally by making the two backends' shapes match rather than
  merely coexist), and apply a size bound (truncate with an explicit marker, not silently) to
  prevent an artifact from producing unbounded `Effect.detail` content now that it is captured
  rather than streamed directly to a real terminal.
- **Explicitly not in scope for this next stage**: the iterative turn contract (Gap 1) itself, any
  change to `Broker`/`Gate`/`CognitiveLoop`, any change to `SeparateProcessBackend`'s existing
  (already-working) capture, any change to what backends are registered or how profiles are
  selected.
- **Why this is the correct next stage, not the iterative turn contract itself**: Gap 2 has no open
  design question — precedent already exists in this exact file (`SeparateProcessBackend`) — while
  Gap 1 does, and design-first is this project's own established, working pattern for exactly this
  kind of question (five-document trail above). Sequencing output capture first also means the
  iterative-turn design stage can assume a real, non-empty result exists to design a feedback
  representation around, rather than designing against a placeholder.
- **Must not change**: `Broker`/`Gate`/`Executor` control flow, `Decision`/`Intent` field shapes,
  `SeparateProcessBackend`'s own behavior, anything in `siphonophore_harness/`.
- **Tests**: a test proving `SameProcessBackend.run()` captures stdout into `Effect.detail` for a
  simple `print()` artifact (mirroring the existing `SeparateProcessBackend` test); a test proving
  nothing reaches the real process's own stdout stream during the call (matching the turn-boundary
  assessment's own `redirect_stdout`-based reproduction method); a test proving a large-output
  artifact is truncated, not silently dropped or left unbounded.

## Explicit Non-Goals

Restated and reconfirmed unchanged by this stage:

- A polished product UI (colors, TUI, web front end).
- A universal agent framework or general orchestration platform.
- Delegation orchestration (deciding *when* to delegate, spinning up a second `CognitiveLoop`
  automatically).
- Kubernetes-specific concepts entering `siphonophore_core` or `siphonophore_harness`'s own
  vocabulary.
- A dedicated `execution_id` distinct from `intent_id`.
- Deciding, in this stage, whether MCP is required, or whether every future operation kind should be
  represented as `artifact_code` — both explicitly named by this stage's own framing as premature.
- Any import of, or reasoning drawn from, `~/research` (not accessed).
- Implementing the iterative turn contract itself in this stage (explicitly reserved for its own
  design stage, per **Ordered Work Sequence** item 2).

---

## Final Report

- **Starting branch/HEAD**: `observe/reference-harness-model-boundary` @
  `38f38dca2b49f9d2c55a7fe78e93c64cc6477b43`, clean working tree.
- **Resulting branch/HEAD**: `reconcile/reference-harness-v1-horizon`, created from the same commit;
  this document is the branch's only change (to be committed as a single commit after this report).
- **Files inspected** (full reads, not excerpts, except where noted): `docs/REFERENCE_HARNESS_ASSESSMENT.md`,
  `docs/REFERENCE_HARNESS_TARGET_DESIGN.md`, `docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md`,
  `docs/REFERENCE_HARNESS_TURN_BOUNDARY_ASSESSMENT.md`, `docs/REFERENCE_HARNESS_TURN_CONTRACT_DESIGN.md`,
  `siphonophore_harness/intent_parsing.py`, `loop.py`, `broker.py`, `outcome.py`, `composition.py`,
  `model_anthropic.py`, `prompts.py`, `siphonophore_core/execution.py`, `examples/repl.py`; `git log`,
  `git show --stat` for `61fbdcf`/`38f38dc`; `README.md` lines 213-267 (excerpt, targeted).
- **Documents found stale/superseded**: `README.md:255-260`'s "Not yet implemented" bullet on the
  reference harness (Gap 5) — factually false today, not yet corrected, already named as
  out-of-scope-for-every-prior-stage. No `docs/REFERENCE_HARNESS_*` document itself was found stale
  — all five accurately describe either a still-open question or work later confirmed complete.
- **Documents found current**: all five `docs/REFERENCE_HARNESS_*` documents' factual claims about
  source, at the time each was written, were independently re-verified against present-day source
  and found accurate; none required correction.
- **Major reconciliation conclusions**:
  1. The turn-contract work (`61fbdcf`, `38f38dc`) fully implements Option C and closes four of the
     five reference acceptance scenarios.
  2. The one unmet scenario (URL-inspection) isolates to exactly two gaps, not a broad shortfall.
  3. Those two gaps are not independent, contra a natural reading of the task's own framing: output
     capture (Gap 2) is a hard prerequisite for iterative continuation (Gap 1) to produce a
     meaningful answer, not merely a co-occurring defect — this is this document's one material
     reordering of the candidate work sequence.
  4. Substrate-selection/authority-exposure work the older implementation plan scoped as Stages 3-5
     is substantially already done, via the turn-contract commits rather than as separate staged
     work — only an operator-facing profile-selection flag remains, and it is non-blocking.
  5. No prior design decision conflicts with the north star; the one stale artifact is a `README.md`
     prose bullet, not a design document.
- **Exact next bounded implementation stage**: `same_process` output capture in
  `siphonophore_core/execution.py` (see **Next Bounded Implementation Stage** above) — narrow,
  core-layer, precedented, no open design question, and a structural prerequisite for the
  iterative-turn-contract design stage that should follow it.
- **Files changed this stage**: `docs/REFERENCE_HARNESS_V1_HORIZON.md` (new). No source, test, or
  example file was modified.
- **Tests run**: full portable suite, `.venv`, `pytest -q -m "not k8s_cluster and not
  linux_root_only"` → `272 passed, 43 deselected` (unchanged from `HEAD`, confirming no regression
  from a documentation-only stage).
- **Commit created**: one, on `reconcile/reference-harness-v1-horizon`, containing only this
  document.
- **Worktree cleanliness**: clean before and after, apart from this document.
- **Requires owner decision**: (a) whether to accept the reordering of Gap 1/Gap 2 relative to the
  task's own candidate sequence; (b) the exact bound/representation choices for the iterative turn
  contract, which this document deliberately leaves to its own future design stage rather than
  pre-empting; (c) whether the `README.md` staleness (Gap 5) should be corrected now, opportunistically,
  or held for a later stage as this document recommends.
