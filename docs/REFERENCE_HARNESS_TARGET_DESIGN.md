# Reference Harness Target-State / Delta Design

**Status: TARGET-STATE / DELTA DESIGN.**

This document defines the smallest target-state delta that makes the existing reference harness
(`examples/repl.py` + `siphonophore_harness/`) minimally acceptable to operate and structurally
credible as a foundation for other harnesses, while preserving Siphonophore's current core
architecture wherever possible. It is a design, not an implementation plan: no code changes
accompany it, and mechanism choices are made only where evidence in this repository is sufficient
to make them responsibly.

## 1. Purpose

Answer one question, at the delta level: given `docs/REFERENCE_HARNESS_ASSESSMENT.md`'s finding
that the reference harness's underlying architecture is largely solid and its presentation layer
is the actual deficiency, what is the smallest, most defensible set of changes — classified by
layer and by whether core changes — that closes the gap between "the mechanism exists" and "an
operator, or a future harness builder, can actually see and use it"?

This document does not redesign the reference harness, does not introduce a new framework, and
does not choose CLI syntax, config file shape, or class names beyond what is necessary to state a
requirement precisely.

## 2. Design basis

Primary evidence: `docs/REFERENCE_HARNESS_ASSESSMENT.md` (commit `c3cb69a`), which itself sources
every claim to a file, line range, or test. Its findings were independently re-verified against
current code for this document (not blindly inherited) by direct inspection of:

- `examples/repl.py` (108 lines) — confirmed line-for-line against §3/§5 of the assessment.
- `siphonophore_harness/broker.py`, `loop.py`, `__init__.py` — confirmed against §2, §6, §10.
- `siphonophore_core/mediation.py`, `execution.py`, `policy.py`, `intent.py`, `identity.py`,
  `audit.py` — confirmed against §2, §4, §7, §10.
- `tests/test_harness_structural_proof.py`, `tests/test_harness_broker.py`,
  `tests/test_harness_loop_k8s_cluster.py`, `tests/test_harness_loop_linux.py`,
  `tests/test_authority.py`, `tests/test_execution.py` — confirmed against §4, §6, §8, §11.
- `DESIGN.md` §6–7, "Explicitly open" section (lines 513–565); `README.md` "Project status and
  current direction" and "Not yet implemented or integrated" sections.

One new finding surfaced during this re-verification, not present in the assessment, is recorded
in §10 below: three of the four `execution.py` refusal sites (forged/tampered decision, plain
policy DENY, no backend registered) raise the bare `GateViolation` base class with no distinguishing
subclass, while the fourth sibling condition (artifact digest mismatch) already has its own
subclass (`ArtifactMismatchError`). `tests/test_execution.py` confirms this directly:
`test_forged_decision_refused`, `test_denied_decision_refused`, and
`test_no_backend_registered_for_unknown_execution_class` all assert only
`pytest.raises(GateViolation)` — the base class — because no more specific type exists to assert
against. This is a genuine, narrowly-scoped structural gap, not a REPL presentation problem: any
caller of `siphonophore_core` directly, with no reference harness involved, hits the identical
limitation.

## 3. Design constraints

- No implementation. No code, test, or script files change as part of this stage.
- No core changes except where independently justified by a gap that exists with zero harness or
  presentation code involved (see §10).
- No Kubernetes vocabulary introduced above the substrate boundary.
- No new mandatory harness layer that an external builder must depend on.
- No orchestration platform, no general provenance model, no universal framework.
- No import of, or reasoning drawn from, `~/research`.
- CLI syntax, exact class names, and config file shape are implementation details deferred to
  Stage 3 wherever a requirement-level statement is sufficient here.

## 4. Existing architecture to preserve

| Element | Decision | Rationale |
|---|---|---|
| `Gate` (mint/verify Decision, Order, Authority) | **PRESERVE** | Independently re-verifies every input at every step (`mediation.py:70-237`); no evidence anything here is insufficient for any UX or foundation requirement found. |
| `Executor` / `ExecutionBackend` boundary | **PRESERVE** | `ExecutionBackend` is already the correct extension point (`execution.py:60-70`); `tests/test_harness_loop_k8s_cluster.py:123-128` proves a new substrate composes with zero core changes. |
| `Broker` mediation path (`Intent -> Gate.submit -> Executor.execute -> Effect`) | **PRESERVE WITH HARNESS-SIDE EXPOSURE** | The mediation sequence itself is sound and non-bypassable; what changes (§10) is what `Broker.dispatch()` returns/attaches, not the sequence of calls it makes. `Broker` is harness code (`siphonophore_harness/broker.py`), not core — this exposure change is a harness-layer delta, not a core change. |
| `CognitiveLoop`/core separation | **PRESERVE** | `tests/test_harness_structural_proof.py:16-56` mechanically enforces this; nothing in this design requires widening `CognitiveLoop.__init__`'s accepted parameters beyond `{model, broker, principal_id, authority}`. |
| `ExecutionBackend` substrate boundary | **PRESERVE** | Already the correct seam (§above); Kubernetes vocabulary already confined to `execution_k8s.py`, mechanically enforced by `tests/test_core_no_k8s_vocabulary.py`. |
| Authority/delegation semantics (`issue_order`/`grant_root_authority`/`delegate`) | **PRESERVE** | Independently re-verified at every step (`mediation.py:70-237`); `tests/test_authority.py` exercises the full refusal surface (forged order, scope expansion, depth exhaustion, splicing). No gap found that requires changing this mechanism. |
| Decision integrity model (HMAC over `kind`/`permitted`/`execution_class`/`artifact_digest`/`authority_id`/`order_id`) | **PRESERVE** | Real, adversarially tested (`tests/test_mediation.py`); nothing in this design reads or writes any field not already bound into the token. |
| `Effect` model (`intent_id`, `execution_class`, `detail`) | **PRESERVE WITH HARNESS-SIDE EXPOSURE** | The three fields are sufficient; `detail`'s per-backend shape (§6/§11 of the assessment) is addressed at the harness-normalization layer (§9 below), not by changing `Effect` itself. |
| Belnap reconciliation (`audit.py`) | **PRESERVE** | Real, proven end-to-end with a lying delegated sub-agent (`tests/test_harness_loop_linux.py:226-276`); this design's minimum bar does not require the REPL to exercise it, only that a future harness demonstrating delegation could reach it unmodified. |
| Substrate-neutral core / harness→core dependency direction | **PRESERVE** | Verified by direct `grep` (zero `siphonophore_harness` imports inside `siphonophore_core/`) and `tests/test_core_no_k8s_vocabulary.py`; every design decision below is checked against this in §15 (adversarial review). |

Nothing in this table is weakened, replaced, or routed around by anything that follows.

## 5. Current-state problem summary

Restated from the assessment, re-verified: the presentation layer (`examples/repl.py`'s three
`except` clauses and its two `print()` call sites) collapses distinctions that already exist,
structurally, in core and in the harness API:

- `Decision` is minted by `Gate.submit()` (`mediation.py:70-122`) and held by `Broker.dispatch()`
  before it is ever used, on *every* code path — success, DENY, integrity failure, and
  missing-backend alike (`broker.py:37-39`: `decision = self._gate.submit(...)` always executes
  before `self._executor.execute(decision, intent)`) — but `Broker.dispatch()` returns only
  `Effect` on success and lets a bare exception propagate on refusal, discarding the `Decision` it
  is already holding in both cases.
- DENY, forged/tampered Decision, and no-backend-registered are raised from three separate call
  sites in `execution.py` (lines 143-146, 157-158) but share one exception type, `GateViolation`,
  with no distinguishing subclass — unlike their sibling condition, artifact-digest mismatch, which
  already has one (`ArtifactMismatchError`, `execution.py:53-57`). `examples/repl.py:87-89`
  therefore cannot distinguish them without string-matching, and neither could any other caller.
- `execution_class` and `intent_id` already exist on `Effect` (`intent.py:47-49`) but are printed
  only under `--verbose` (`execution_class`) or never (`intent_id`) (`examples/repl.py:94-102`).
- Substrate selection (`Executor(gate, backends=...)`) and authority/delegation
  (`Gate.issue_order/grant_root_authority/delegate`, `Broker.dispatch(authority=...)`,
  `CognitiveLoop(authority=...)`) are both fully composable today (`tests/test_harness_loop_k8s_cluster.py:123-128`,
  `tests/test_harness_broker.py:57-84`, `tests/test_harness_loop_linux.py:278-321`) but
  `examples/repl.py` calls neither — it constructs exactly one fixed, authority-less wiring
  (`examples/repl.py:62-65`).

## 6. Minimum acceptable operator UX

Each of the ten candidate requirements from the task framing, tested against current code:

1. Start without reading internal source. **NOT REQUIRED FOR MINIMUM BAR to change** — already
   true (`examples/repl.py:10-19` documents setup completely).
2. Understand what execution capability is available. **REQUIRED** — today neither discoverable
   nor selectable (assessment §9); target state must let an operator learn, at minimum, what
   substrate(s) their session can reach.
3. Submit a normal request. **NOT REQUIRED FOR MINIMUM BAR to change** — already works.
4. Understand enough authority context to know what a request is attempted under. **REQUIRED, at a
   bounded level** — not full delegation UX (see §12), but at minimum: whether the session is
   running authority-less or under a held `Authority`, and if the latter, its `principal_id` and
   scope. Full delegation *orchestration* remains deferred (§12).
5. Distinguish ALLOW+success / DENY / integrity-or-verification rejection / substrate-or-backend
   failure / execution failure / malformed-input. **REQUIRED** — this is the central, highest-value
   delta; see §9–10.
6. Identify what concrete execution path/substrate was used. **REQUIRED** — `execution_class`
   already exists on every `Effect`; it must move out from behind `--verbose`.
7. See sufficient correlation identity to connect a request to its result. **REQUIRED** —
   `intent_id` already exists on `Effect`; it is never printed today, even in `--verbose` mode.
8. Understand, at a useful level, what Siphonophore decided and what actually happened, as two
   separate facts. **REQUIRED** — this is requirement 5 restated at the conceptual level; see §10.
9. Use the harness without learning Siphonophore's internal Python class structure. **REQUIRED** —
   satisfied once 5-8 are satisfied with plain-language labels, not exception class names.
10. Obtain machine-consumable structured state from reusable harness logic, not terminal-string
    scraping. **REQUIRED for the *composition* layer, NOT REQUIRED that the REPL itself emit JSON**
    — the requirement is that a second front end can get the same structured facts (decision
    projection, outcome category, execution_class, intent_id) by calling the same harness-level
    composition the REPL calls, not by parsing what the REPL prints. See §7–8.

Explicitly held to REQUIRED only, not polished: no color, no TUI, no formatting beyond plain
labeled text; no new commands beyond what's needed to select a substrate/authority once at
construction time (§11-12); no discovery UI beyond one flag/one printed line.

## 7. Target composition model

```
operator (human, via terminal)
        |
        v
presentation (examples/repl.py)         <- argparse, input() loop, rendering a structured
        |                                   outcome into plain text; owns no decision logic
        v
reusable harness composition             <- CognitiveLoop + Broker + a small, explicit wiring
(siphonophore_harness/)                     step (substrate selection, optional authority) that
        |                                   is already possible today but only inline in repl.py
        v
Broker.dispatch(intent, authority=...)   <- WIDENED: returns a structured outcome carrying both
        |                                   Decision-derived facts and Effect, or raises with a
        |                                   Decision-derived projection attached (§10)
        +--> Gate.submit()  --> Decision
        +--> Executor.execute() --> Effect
                  |
                  v
        ExecutionBackend (substrate boundary, unchanged)
```

No new mandatory class is assumed. "Reusable harness composition" names a requirement — a place
outside `examples/repl.py` where (a) the default wiring, (b) substrate selection, and (c) the
optional authority a session runs under are assembled once — not a prescribed new class hierarchy.
Whether this becomes one function, one small dataclass of constructor arguments, or a documented
recipe is Stage 3's decision (§19).

## 8. Responsibility boundaries

| Capability | Owner | Current state | Target delta |
|---|---|---|---|
| Policy decision semantics (`permitted`, `execution_class`) | CORE | `policy.py` | None |
| Execution verification (HMAC re-check, artifact digest re-check) | CORE | `execution.py:143-154` | None |
| Distinct exception *types* for DENY / forged-decision / no-backend | CORE | Collapsed into bare `GateViolation` (§2, §10) | **New, narrow subclasses** — the one core-level delta in this design |
| Backend selection *mechanism* (`Executor(gate, backends=...)`) | CORE | Already sufficient (`execution.py:130-138`) | None |
| Backend selection *exposure to an operator* | REFERENCE-HARNESS COMPOSITION | Does not exist (`examples/repl.py:63-64` hardcodes it) | New: a discoverable, one-time-at-construction choice (§11) |
| Backend/substrate configuration (namespace, image, uid ranges, etc.) | SUBSTRATE-SPECIFIC CONFIGURATION | Lives below the backend boundary already (`execution_k8s.py`, `execution_uid_cgroup.py`) | None |
| Authority construction (`issue_order`/`grant_root_authority`/`delegate`) | CORE | `mediation.py:147-237` | None |
| Deciding *whether* a session holds an authority, and displaying it | REFERENCE-HARNESS COMPOSITION / PRESENTATION | Does not exist | New: optional, at construction time (§12) |
| Delegation orchestration (deciding *when* to delegate, spinning up a second loop) | OUT OF SCOPE for this design | Does not exist anywhere (test code only) | Explicitly deferred (§12, §17) |
| Result/outcome normalization (Decision projection + Effect + outcome category) | REFERENCE-HARNESS COMPOSITION | Does not exist; `examples/repl.py`'s `except` clauses do this ad hoc and lossily | New: a small, explicit mapping from existing core types to a stable outcome category (§9-10) |
| Terminal rendering | PRESENTATION | `examples/repl.py:94-102` | Rewritten to render the new structured outcome, not exception strings |
| Verbose diagnostics (raw completion, raw `detail` dict) | PRESENTATION | `examples/repl.py:99-102` | Preserved as-is; additive only |
| Correlation identifiers (`intent_id`) | CORE (data) / PRESENTATION (visibility) | Exists in core, invisible in presentation | Presentation change only |
| Kubernetes configuration (namespace, pod spec, kubectl) | SUBSTRATE-SPECIFIC CONFIGURATION | `execution_k8s.py` only | None — must stay there |

Nothing here moves policy, verification, or authority semantics out of core, and nothing moves
substrate-specific vocabulary above the boundary. The one core-owned *delta* (new exception
subclasses) does not change what is decided, verified, or authorized — only what type a caller can
catch to learn which of three already-distinct code paths produced a refusal.

## 9. Structured outcome design

Starting from distinctions **already present in current semantics** (assessment §7, re-verified
directly against `execution.py`, `mediation.py`, `intent_parsing.py`, `identity.py`):

| Distinction | Exists structurally today? | Reachable via `Broker.dispatch()` in ordinary (non-test) use? |
|---|---|---|
| Malformed/hostile completion | Yes — `IntentParseError` (`intent_parsing.py:40-43`), raised *before* `Broker.dispatch()` is even called (`loop.py:65-67`) | N/A — happens upstream of Broker |
| Forged/mismatched authority (pre-Decision) | Yes — `GateViolation`, raised inside `Gate.submit()` before any `Decision` is minted (`mediation.py:98,100`) | Yes, if a caller passes an `Authority` that fails re-verification or belongs to a different principal |
| Policy DENY | Yes — `Decision.permitted is False`, raised as `GateViolation` in `Executor.execute()` (`execution.py:145-146`) | Yes — the common, expected refusal case |
| Decision failed re-verification (forged/tampered/downgraded) | Yes — `GateViolation` (`execution.py:143-144`) | Effectively no, *through Broker's own path* — Broker uses the same `Decision` object `Gate.submit()` just minted, immediately, with nothing in between that could tamper it. Only reachable by a caller that constructs `Executor.execute()` calls directly (as `tests/test_execution.py:79-86` does) — i.e., a caller bypassing `Broker`. |
| Artifact digest mismatch | Yes — `ArtifactMismatchError`, already a distinct subclass (`execution.py:53-57,150-154`) | Effectively no, through Broker's own path, for the identical reason (the same frozen `Intent` object is hashed at mint time and used at execution time) |
| No backend registered for `execution_class` | Yes — `GateViolation` (`execution.py:157-158`) | **Yes, and realistically** — e.g. a policy mapping that resolves to `"uid_cgroup"` against an `Executor` that only registered the two portable backends |
| Check-in/execution-identity failure | Yes — `IdentityError`, a third, separate exception family (`identity.py:31-34`) | Only if a check-in backend is registered; not reachable from the current REPL wiring at all |
| Backend/artifact execution failure | Yes — `ExecutionError` (`execution.py:33-36`), one family covering both "the substrate itself failed to invoke" and "the artifact ran but exited nonzero/raised" | Yes |
| Success | Yes — `Effect` returned | Yes |

**What this table changes about the assessment's framing:** the assessment (§7) correctly found
that core already represents every distinction structurally, but treated the DENY /
forged-decision / no-backend collapse as purely a presentation problem. Re-inspection shows it is
*not purely presentation*: those three conditions share the literal same Python exception type
today (bare `GateViolation`), with no subclass to distinguish them — unlike artifact-mismatch,
which already has one. A caller cannot mechanically distinguish DENY from a forged-decision
rejection from a missing-backend condition without parsing the exception's message string,
regardless of whether a reference harness is involved at all. This is the one place this design
recommends a (narrow, additive) core change — see §10.

**Design decision — outcome categories.** The reusable harness-composition layer should classify
every `Broker.dispatch()` result/exception into one of a small, closed set of categories, built
entirely from types that already exist (plus the three new subclasses from §10):

- `executed` — an `Effect` was returned.
- `input_rejected` — `IntentParseError` (pre-mediation; no `Intent` ever existed).
- `authority_rejected` — `GateViolation` raised before a `Decision` was minted (forged/mismatched
  authority); no `Decision` to attach.
- `denied` — `GateViolation`/`PolicyDenied` where `decision.permitted is False`.
- `integrity_rejected` — `ArtifactMismatchError` or `GateViolation`/`DecisionVerificationError`
  (Decision itself failed re-verification).
- `backend_unavailable` — `GateViolation`/`NoBackendRegisteredError`.
- `identity_rejected` — `IdentityError` (reachable only where a check-in backend is registered).
- `execution_failed` — `ExecutionError`.

This is **DESIGN DECISION**, not IMPLEMENTATION DETAIL: the category *set* and what each maps from
is a real classification choice; the exact Python shape (string enum, `Literal`, small dataclass)
is IMPLEMENTATION DETAIL for Stage 3. This satisfies UX requirement 5 (§6) and machine-consumability
requirement 10 (§6) without inventing any new failure condition — every category is a direct,
named alias for a distinction that already exists in `siphonophore_core`.

## 10. Decision / Effect visibility design

**Current behavior**, re-confirmed directly: `Broker.dispatch()` (`broker.py:37-39`) always mints a
`Decision` via `Gate.submit()` before calling `Executor.execute()`, on every path — ALLOW, DENY,
forged-decision, artifact-mismatch, and no-backend all have a `Decision` already sitting in a local
variable inside `dispatch()` at the moment `Executor.execute()` raises (the one exception:
authority-rejected, §9, where `Gate.submit()` itself raises before minting one). `Broker` is
**harness code** (`siphonophore_harness/broker.py`), not core — this matters directly for evaluating
the options below, since it means the "does this touch core" axis is already favorable for any
option that only changes `Broker`.

**Options considered:**

| Option | Core impact | Compatibility | Abstraction quality | Info preservation | Reuse | Test impact | Presentation-into-core risk |
|---|---|---|---|---|---|---|---|
| A — widen `Broker.dispatch()`'s return contract | None (Broker is harness code) | Breaks callers that assume a bare `Effect` return | Good if the widened shape is a plain composed value, not a grab-bag | Full — nothing discarded | High — every future harness reaching through Broker gets it | Every existing `Broker.dispatch()`-success-path test needs updating | Low |
| B — preserve `Broker.dispatch()`, expose Decision through an existing/new harness-level seam | None | High | Depends entirely on what the seam is | Depends | Depends | Depends | Depends |
| C — a harness-level operation/result object composing existing core outputs, without changing core semantics, that does *not* go through `Broker.dispatch()` | None | High (additive) | Poor as evaluated: the only way to compose `Decision`+`Effect` without going through `Broker` is to call `Gate.submit()`/`Executor.execute()` directly — exactly what assessment §10 already found "defeats the point of Broker existing at all" | Full | Duplicated composition logic risks divergence from `Broker`'s own sequence | New tests needed for the duplicate path | Low, but duplicative |
| D — operator does not need `Decision`; expose only a selected projection | None | High | Good — avoids leaking a cryptographic token (`Decision.token`) into presentation code for no purpose | Partial by design (deliberately partial) | High | Same as whichever option carries it | Lowest |
| E — combine A (mechanism: widen what Broker exposes) + D (shape: expose a projection, not the raw `Decision`) | None | Same as A | Best of both: minimal surface, no raw core object with a meaningless-to-presentation `token` field leaking upward | Full for every field that matters to an operator | High | Same as A | Low |

**Selected direction: Option E (A for mechanism, D for shape) — DESIGN DECISION.**

Rationale, addressing the task's explicit warning ("operator needs to understand the decision"
does NOT automatically imply "Broker must return Decision"): the design does **not** propose
returning the raw, cryptographically-tokened `Decision` object to presentation code. It proposes
that `Broker.dispatch()`:

- On success, return a small composed value carrying the existing `Effect` plus a **projection** of
  the `Decision` fields an operator or richer front end can actually use: `permitted`,
  `execution_class`, `authority_id`, `order_id`. (`artifact_digest` and `token` are deliberately
  excluded from the projection — the digest is already implicitly confirmed by successful
  execution, and the token has no meaning outside `Gate`.)
- On refusal, attach the same projection to the raised exception when a `Decision` was actually
  minted before the refusal occurred (every category in §9 except `input_rejected` and
  `authority_rejected`), and leave it absent (not fabricated) when no `Decision` exists yet — this
  follows the "expose the best currently-valid identifier honestly" principle used for correlation
  identity in §13, applied here to decision state.

This is classified as a **harness-layer** change (not core) because `Broker` lives in
`siphonophore_harness/`. It does not weaken `Broker`'s "one capability — `dispatch(intent)` — and
nothing else" framing (`broker.py:1-4`): the capability remains exactly one method with the same
inputs; only what that one method's return/raise carries is widened. No new method is added to
`Broker`.

**What this does NOT resolve, left OPEN:** whether `Broker.dispatch()` should switch from its
current raise-on-refusal pattern to a uniform return-a-result pattern (never raising) is a bigger
behavioral change — it would require rewriting every existing `Broker`/`CognitiveLoop` test that
currently uses `pytest.raises(GateViolation)`, and `CognitiveLoop.step()`'s own documented
contract ("a denied/refused dispatch... propagates rather than being swallowed here", `loop.py:57-61`)
would need re-justifying. This design keeps raise-on-refusal and only widens what is attached to
what already raises/returns — the minimal delta that satisfies the UX requirement.

## 11. Substrate selection/configuration design

**Target requirement (DESIGN DECISION on requirement, mechanism-neutral):**

- Substrate/backend selection must be **discoverable**: an operator must be able to learn, without
  reading source, which execution classes a given session's `Executor` has registered.
- Substrate/backend selection must be **configurable at construction time** — i.e., when the
  reusable harness composition (§7) builds its `Executor`/`Policy`/`Gate`, not mid-session. No
  evidence was found in the assessment or in re-inspection that request-time substrate switching is
  needed for the minimum bar; `tests/test_harness_loop_k8s_cluster.py` and every other substrate
  test construct one fixed wiring and use it for the test's duration, consistent with this.
- A session exposes exactly **one** execution profile (one `Gate`/`Executor`/`Policy` triple) for
  the minimum bar. Multiple simultaneous profiles per session is a richer-future-harness
  possibility (§16), not required here.
- The operator needs to know, at minimum, which execution classes are registered and which
  `consequence -> execution_class` mapping is active — not the backend's internal configuration
  (namespace, uid ranges, image), which remains substrate-specific (§8) and stays out of the
  reference harness's own concern entirely.
- The reusable composition surface a future front end should call is exactly what already exists
  and needs no core change: `Executor(gate, backends={...})`, `ConsequencePolicy(mapping={...})`.
  What's missing is a reference-harness-level convenience that constructs one or more *named,
  ready-to-use* wirings (e.g., "portable-only" — today's default — and "with uid_cgroup" or "with
  k8s_pod" where the environment supports it) so a second front end does not have to hand-copy
  `examples/repl.py:62-65`.

**CORE impact: none.** `ExecutionBackend`, `Executor(gate, backends=...)`, and
`ConsequencePolicy(mapping=...)` are already sufficient (assessment §9, re-confirmed). Kubernetes
identifiers (`Pod`, `Namespace`, `pod_id`) must not appear in this composition layer's own
vocabulary — only backend *names* (`"same_process"`, `"k8s_pod"`, etc., which are already
core-defined execution-class strings, not Kubernetes objects) need to surface.

**Not decided here (IMPLEMENTATION DETAIL, Stage 3):** exact flag/config syntax, whether the
"named wiring" convenience lives as a function, a small dataclass, or a documented recipe in
`siphonophore_harness/`.

## 12. Authority and delegation design

Answering the task's specific questions, grounded in assessment §8 and re-verified against
`mediation.py`, `broker.py`, `loop.py`:

- **Must the operator see `authority_id`?** REQUIRED when a session runs under a held `Authority`;
  NOT REQUIRED (and should not be shown) for the ordinary authority-less path, to avoid cluttering
  the common case with an always-`None` field.
- **Must the operator see scope?** REQUIRED under the same condition — an operator running under a
  delegated `Authority` needs to know what it's scoped to, otherwise "authority context is
  understandable enough to operate safely" (UX requirement 4, §6) is not met.
- **Must remaining delegation depth be visible?** PROBABLY REQUIRED under the same condition, for
  the same reason — it is already a field on `Scope` (`authority.py`), not a new concept.
- **Should ordinary single-user operation hide most authority mechanics unless requested?** YES —
  DESIGN DECISION. The authority-less path (`Gate.submit(intent)`, unchanged since before the
  parameter existed, `mediation.py:70-73`) remains the default; authority-related fields appear
  only in sessions actually constructed with one.
- **How does the harness receive/construct authority?** At session-construction time only (not
  mid-session), by the reusable harness composition (§7) — the same layer responsible for
  substrate selection (§11). `CognitiveLoop(..., authority=...)` and `Broker.dispatch(intent,
  authority=...)` are already the correct, sufficient interface (`tests/test_harness_loop_linux.py:278-321`
  proves this composes correctly for two independently constructed loops sharing one
  `Gate`/`Broker`). No new core parameter is needed.
- **Is the current authority-less convenience path appropriate for the reference UX?** YES —
  PRESERVE. It is the right default for a single-operator reference harness; requiring every
  session to construct an `Order`/`Authority` chain just to send one message would regress
  requirement 1 (§6, "start without reading internals").
- **Minimum support needed to demonstrate delegation without a general orchestrator?** The minimum
  is: the reusable composition layer must be *able* to construct a session that holds a
  pre-supplied `Authority` (obtained however the operator chose to obtain it — e.g., by a documented
  script/recipe using `Gate.issue_order`/`grant_root_authority`/`delegate` directly, mirroring what
  `tests/test_harness_loop_linux.py:278-321` already does). Deciding *when* to delegate, and
  constructing a second, independently running `CognitiveLoop` automatically, is orchestration —
  explicitly named open, unbuilt work by both `DESIGN.md:551-559` and `README.md:242-244`, and out
  of scope here.
- **Should delegation be a minimum-bar capability or a documented advanced path?** DESIGN DECISION:
  **documented advanced path.** Minimum bar requires only that a session *can* run under a
  pre-constructed `Authority` and that doing so is visible (above); it does not require the
  reference harness to drive delegation end-to-end interactively. This matches `DESIGN.md`'s and
  `README.md`'s own framing of orchestration as separate, not-yet-built work, and avoids turning the
  reference harness into a multi-agent orchestrator (an explicit non-goal, §14).

**CORE impact: none.** Every mechanism named above already exists and is already sufficient.

## 13. Correlation/identity visibility

**Target:** the operator must see `intent_id` (already generated fresh per turn by
`parse_intent()`, `intent_parsing.py:86`, and already carried on both `Decision.intent_id` and
`Effect.intent_id`) for every turn, by default — not only in `--verbose` mode. This is the "best
currently-valid identifier," exposed honestly, per the task's explicit instruction not to invent a
new identity abstraction for UX purposes alone.

**Not resolved here, and not required to be:** `intent_id` vs. a dedicated `execution_id`. The
project's own documentation already treats a dedicated execution identity as open work (outside
this document's scope — see `docs/EXECUTION_K8S.md`'s and `DESIGN.md`'s own disclosed limitations
around execution-identity mechanisms). What a future concurrency requirement could invalidate:
`intent_id` is unique per dispatched intent, but if a single intent is ever retried or re-dispatched
against the same backend (not something any current code path does), `intent_id` alone would no
longer uniquely identify one *execution attempt* — that is exactly the gap a future dedicated
`execution_id` would close, and exactly why this document declines to pre-empt it.

## 14. External-builder composition model

A team building a web harness, a different CLI, an enterprise service, or an orchestrator should be
able to learn, from the target-state reference harness, without depending on `examples/repl.py`,
its output strings, or any reference-harness-specific presentation convention:

- How to compose `Gate`/`Broker`/`Executor` — from `siphonophore_harness/broker.py`'s docstring and
  the reusable composition layer (§7), not from reading `repl.py`.
- How to register/select execution backends — from `Executor(gate, backends=...)` directly (§11);
  the reference harness's own "named wiring" convenience is a convenience, not the only way in.
- How to supply authority — from `Gate.issue_order/grant_root_authority/delegate` and
  `Broker.dispatch(authority=...)`/`CognitiveLoop(authority=...)` directly (§12); nothing about
  authority supply is reference-harness-specific.
- How to invoke a workload — `Broker.dispatch(intent, authority=...)`, unchanged in shape, only
  widened in what it returns/attaches (§10).
- How to interpret outcomes — the outcome-category classification (§9) is defined entirely in terms
  of `siphonophore_core` exception types (plus the projection from §10); a builder can implement the
  identical classification against `siphonophore_core` alone, without importing anything
  reference-harness-specific, because every type it switches on already lives in core.
- How to preserve relevant identifiers — `intent_id`, already on `Effect`/`Decision` (§13).
- How to keep presentation independent from core semantics — by observing that the reference
  harness's own presentation layer (`examples/repl.py`) contains no logic a second front end would
  need (assessment §5's finding, re-confirmed: nothing about REPL's `print()` formatting blocks a
  second front end from being built against the same classes).

**What makes this a credible exemplar, concretely:** every capability in the table above is reached
through a public constructor or public method that already exists in `siphonophore_core` or
`siphonophore_harness`, with no reference-harness-only concept in between. The only thing this
design adds that an external builder would need to *notice* (not depend on) is the outcome-category
classification (§9) and the Decision projection (§10) — and both are defined purely in terms of
core types, so a builder can reimplement the identical classification without touching
`examples/repl.py` at all, exactly as `tests/test_harness_loop_k8s_cluster.py` already demonstrates
for substrate wiring.

## 15. Current → target delta table

| Area | Current state | Target state | Responsible layer | Core change? | Implementation implication |
|---|---|---|---|---|---|
| DENY / forged-decision / no-backend distinction | One shared `GateViolation` type, no subclass | Three narrow, additive subclasses of `GateViolation` (paralleling `ArtifactMismatchError`) | CORE | **Yes — narrow, additive, backward-compatible** | `execution.py` raise sites change class; every existing `except GateViolation` still catches all three; `tests/test_execution.py`'s three affected tests can optionally tighten their `pytest.raises(...)` target |
| Decision visibility | Discarded after use by `Broker.dispatch()` | Projection (`permitted`, `execution_class`, `authority_id`, `order_id`) returned on success, attached to raised exceptions on refusal (where minted) | REFERENCE-HARNESS COMPOSITION (`broker.py`) | No | `Broker.dispatch()`'s return type changes; every direct caller of `Broker.dispatch()` (tests, `CognitiveLoop`) needs updating to the new return shape |
| Outcome category | Presentation-layer string matching / bare exception types | Closed set of named categories derived from existing core types | REFERENCE-HARNESS COMPOSITION | No | New, small classification function; no new exception types beyond the CORE row above |
| `execution_class`/`intent_id` visibility | `--verbose`-only / never | Default-visible, every turn | PRESENTATION | No | `examples/repl.py` rendering change only |
| Substrate discoverability | None | One-line, session-start disclosure of registered execution classes and the active consequence mapping | REFERENCE-HARNESS COMPOSITION / PRESENTATION | No | New composition-layer accessor; new print statement |
| Substrate selection | Hardcoded to two portable backends | Selectable at construction time via a documented, named-wiring convenience | REFERENCE-HARNESS COMPOSITION | No | New helper(s) in `siphonophore_harness/`; `examples/repl.py` gains a flag (exact syntax: Stage 3) |
| Authority visibility | None | Shown only when a session holds one; hidden by default | PRESENTATION / REFERENCE-HARNESS COMPOSITION | No | New optional session-construction path; new conditional rendering |
| Delegation orchestration | Test-only | Unchanged — explicitly deferred | N/A | No | None in this arc |
| `Effect.detail` cross-backend shape | Ad hoc per backend | Unchanged at the core level; a thin, additive harness-side rendering convention may format known common keys uniformly for display, without requiring backends to agree on a shape | PRESENTATION (rendering only) | No | Cosmetic only; `Effect.detail` itself is untouched |

## 16. Minimum acceptance criteria

The target state is met when, without reading `siphonophore_core` or `siphonophore_harness`
source:

1. An operator can learn what execution classes/backends a session has available.
2. An operator sees `execution_class` and `intent_id` for every turn by default.
3. An operator can visibly distinguish: executed / denied / integrity-or-verification-rejected /
   backend-unavailable / execution-failed / malformed-input, using plain labels, not exception class
   names or raw messages.
4. An operator running under a held `Authority` can see that they are, and under what scope; an
   operator not running under one sees no authority-related clutter.
5. A future front end can obtain every fact in 1-4 by calling the same reusable harness composition
   the reference presentation calls, without parsing printed text.
6. `tests/test_harness_structural_proof.py` and `tests/test_core_no_k8s_vocabulary.py` continue to
   pass unmodified in spirit (the non-bypass and no-k8s-vocabulary invariants still hold) once
   Stage 3 implements this design.
7. The full portable test suite continues to pass (with additions/updates for the `Broker`
   return-shape change and the new exception subclasses), with no regression in the
   `k8s_cluster`/`linux_root_only`-marked tests' own logic (only their assertions against
   `Broker`'s changed return shape, where applicable).

## 17. Explicit non-goals

Confirmed unchanged from the assessment, and not reopened by anything in this design:

- A polished product UI (colors, TUI, web front end).
- A universal agent framework or general orchestration platform.
- Delegation orchestration (deciding *when* to delegate, spinning up a second loop automatically).
- Kubernetes-specific concepts entering core or the reusable composition layer's own vocabulary.
- A dedicated `execution_id` distinct from `intent_id`.
- `CheckinRegistry` reuse for Kubernetes, `AgentWatch` integration, ServiceAccount policy design,
  observer API design, managed-cluster architecture.
- A shared cross-backend shape for `Effect.detail` at the core level (only cosmetic, presentation-side
  formatting of known common keys is in scope, and only if it turns out to be trivial in Stage 3).
- Recursive/general provenance beyond `Order`/`Authority`/`Scope`.
- A REST API or any remote service protocol.
- Anything drawn from `~/research` (not accessed during this stage).

## 18. Open design questions

1. **Exact shape of the widened `Broker.dispatch()` return value** (a small dataclass vs. a
   `NamedTuple` vs. attaching to `Effect` itself via a wrapper). *Why it matters:* affects every
   caller's ergonomics. *What evidence is missing:* none functionally — this is a naming/shape
   preference, not a semantic question. *Blocking?* No — Stage 3 can decide this as an
   implementation detail without reopening this design.
2. **Whether the "named wiring" substrate-selection convenience belongs in
   `siphonophore_harness/__init__.py`, a new module, or stays a documented recipe** (assessment
   §16's open question 5, restated). *Why it matters:* affects how discoverable the convenience is
   to an external builder. *What evidence is missing:* no evidence either way is materially better;
   a judgment call. *Blocking?* No.
3. **Whether `examples/repl.py` should gain an installed CLI entry point
   (`[project.scripts]`)** — named NICE-TO-HAVE by the assessment (§5), not re-elevated here.
   *Blocking?* No, and likely stays deferred past Stage 3 too unless requested.
4. **Whether the three new `GateViolation` subclasses (§10, §15) should be introduced in the same
   implementation pass as the harness-level Decision/outcome work, or as a separate, smaller,
   earlier change** (since the core change is independently justifiable and low-risk on its own).
   *Why it matters:* sequencing affects how Stage 3 is broken into reviewable increments. *What
   evidence is missing:* none — this is a planning/sequencing choice for Stage 3, not a design
   question. *Blocking?* No.

No open question above blocks Stage 3 from producing a concrete implementation plan; each is
either a naming/shape detail or a sequencing choice.

## 19. Implementation-planning inputs

What Stage 3 will need, beyond this document:

- A concrete Python shape for the widened `Broker.dispatch()` return value and for the
  Decision-projection attached to raised exceptions (open question 1).
- A concrete list of the three new core exception subclass names and their exact raise-site
  mapping in `execution.py` (§10, §15) — names themselves are not chosen here to avoid
  pre-empting a naming convention Stage 3 should set deliberately, matching `ArtifactMismatchError`'s
  existing precedent.
- A decision on where the substrate-selection and authority-supply composition helpers live in
  `siphonophore_harness/` (open question 2).
- An enumeration of exactly which existing tests (`tests/test_harness_broker.py`,
  `tests/test_harness_loop_linux.py`, `tests/test_harness_loop_k8s_cluster.py`,
  `tests/test_execution.py`) need updating for the `Broker.dispatch()` return-shape change, and
  which of `test_execution.py`'s three affected tests should tighten their `pytest.raises(...)`
  target to the new subclasses.
- Exact `examples/repl.py` flag syntax for substrate selection and optional authority supply (not
  decided here per this stage's own scope boundary).
- Confirmation that `144 passed, 43 skipped` remains the passing baseline immediately before Stage
  3 begins (already re-verified as of this document: see final report).
