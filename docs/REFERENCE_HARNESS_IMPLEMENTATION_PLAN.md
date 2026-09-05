# Reference Harness Implementation Plan

**Status: IMPLEMENTATION PLAN — APPROVED DESIGN INPUT, NO IMPLEMENTATION YET.**

## 1. Purpose

Translate `docs/REFERENCE_HARNESS_TARGET_DESIGN.md` (commit `7b834ff`) into an ordered set of
bounded, hands-free implementation stages, having first mechanically re-verified — against current
source and tests, not inherited by trust — the two claims the target design's own soundness rests
on: that widening `Broker.dispatch()` really has no core impact, and that the proposed new
exception subclasses are independently justified rather than presentation convenience. No code,
test, or example file changes as part of this stage.

## 2. Inputs and verified baseline

- Repository: `~/work/siphonophore`, VM `sipho-integration`, user `shunhonda`.
- Branch `design/reference-harness-ux-foundation`, HEAD `7b834ff353d295c9320c78250174c78ea08c252f`,
  history includes `c3cb69a` (assessment) and `7b834ff` (target design). Working tree clean.
- `origin/main` and remote `refs/heads/main` both `311f49852c8aab5e14fcd534bfe2e635efb86ba4` —
  match.
- Portable suite baseline, re-run for this stage: `144 passed, 43 deselected` (`pytest -q -m "not
  k8s_cluster and not linux_root_only"`) — matches the design document's own recorded baseline
  (§19).
- `~/research` not accessed at any point in this stage.
- Every file/line citation below was independently re-read from current source during this stage
  (`siphonophore_core/{execution,mediation,policy,intent,authority,identity,audit}.py`,
  `siphonophore_harness/{broker,loop,__init__,intent_parsing}.py`, `examples/repl.py`, and the test
  files named throughout) — not copied from the assessment or target design without verification.

## 3. Design corrections/refinements discovered during planning

No target-design reopen is required. Two narrow refinements were found; neither changes the
selected direction, and neither is written into `REFERENCE_HARNESS_TARGET_DESIGN.md` because
neither is a factual error in it — both are completions of ambiguity the design itself explicitly
left to this stage.

**Refinement 1 — a fifth `execution.py` raise site the design's "three of four" accounting
does not name.** `Executor.execute()` (`execution.py:141-142`) also raises bare `GateViolation`
when `decision.intent_id != intent.intent_id or decision.kind != intent.kind` ("decision does not
correspond to this intent") — a condition distinct from, and raised before, the "decision failed
Gate verification" check the design's §2/§9 does name (`execution.py:143-144`). No test in the
current suite exercises this raise site at all (confirmed by grep: no test constructs a
mismatched-decision/intent pair). Like the "forged/tampered decision" case the design already
analyzes, this condition is practically unreachable through `Broker.dispatch()`'s own path (Broker
always mints a `Decision` from, and immediately executes, the *same* `Intent` object — see
`broker.py:37-39`) and is reachable only by a caller bypassing `Broker` to call `Executor.execute()`
directly, exactly as `tests/test_execution.py:79-87` already does for the sibling case. This does
not affect the outcome-category set (§9 of the design) because it is not reachable via the sanctioned
composition path any of the eight categories are defined over. Classified **PRESENTATION
ONLY / OUT OF SCOPE for this arc** — recorded here so Stage 1 does not silently invent a fourth
subclass without noticing this site, and so nobody mistakes its absence from the category table for
an oversight later. Non-blocking.

**Refinement 2 — `CognitiveLoop.step()`'s own return value must carry whatever `Broker.dispatch()`
is widened to return, or the REPL gets nothing.** The target design (§15) states "every direct
caller of `Broker.dispatch()` (tests, `CognitiveLoop`) needs updating to the new return shape" but
does not spell out that this is not optional plumbing: `examples/repl.py` never calls
`Broker.dispatch()` directly today (`examples/repl.py:83`, `loop.step(user_message)`), and
`CognitiveLoop.step()` currently just returns whatever `self._broker.dispatch(...)` gave it
unchanged (`loop.py:67,70`). If Stage 2 widens `Broker.dispatch()`'s return value but
`CognitiveLoop.step()` is "updated" only by unwrapping it back down to a bare `Effect`, the REPL
(Stage 4) has nothing richer to render and the entire arc's UX goal is not met through its own
primary call path. This is not a contradiction to correct in the design document — it is exactly the
kind of implementation-shape decision §18's open question 1 already deferred to this stage. The
implementation-planning conclusion (§5 below) is that the cleanest resolution is for the new
result type to be **Effect-compatible by delegation** (same public attribute surface as `Effect`:
`intent_id`, `execution_class`, `detail`), so `CognitiveLoop.step()` requires no logic change at all
(it already does `return effect` verbatim) and `_describe_effect()` (`loop.py:73-74`) keeps working
unmodified. This is recorded as a **Stage 2 implementation requirement**, not a design correction.

## 4. Implementation invariants

Carried forward from the target design (§4, §17) and re-confirmed against current source; every
stage below is checked against these:

1. `siphonophore_core` remains substrate-neutral — no `siphonophore_harness` import anywhere in
   `siphonophore_core/` (re-verified: zero hits).
2. Reference harness remains a consumer, not a mandatory layer.
3. Core never depends on harness (same check as #1).
4. External harness builders do not need `examples/repl.py`.
5. Kubernetes vocabulary stays below the substrate boundary (`tests/test_core_no_k8s_vocabulary.py`,
   confirmed passing, confirmed to scan every `siphonophore_core/*.py` except `execution_k8s.py` by
   both a capitalized-noun/keyword regex and an AST-level dataclass-field/parameter-name scan).
6. Decision integrity checks (`Gate._mint`/`verify`, `mediation.py:52-68,124-135`) are unchanged.
7. No bypass around Gate → Executor mediation is introduced (`Broker.dispatch()` still always calls
   `self._gate.submit(...)` before `self._executor.execute(...)`, unconditionally, on every path).
8. Authority/delegation semantics unweakened (`mediation.py:70-237` unchanged; no new core
   parameter).
9. `execution_id` stays OPEN — not introduced as a concept distinct from `intent_id` anywhere in
   this arc (confirmed: the only `execution_id` identifiers in the codebase today belong to
   `execution_uid_cgroup.py`/`identity.py`'s already-existing, unrelated check-in/provisioning
   mechanism, not something this arc's outcome/correlation work touches or extends).
10. `AgentWatch` remains external — no reference in any file this plan touches.
11. No new mandatory framework layer — the new harness-level result type and wiring helper are
    additive; nothing requires an external builder to adopt them.
12. Terminal rendering does not become semantic state — the outcome-category classification (§9 of
    the target design) is defined entirely over `siphonophore_core` exception/return types, computed
    before any `print()` call, not derived from what a string would look like once rendered.

## 5. API/contract analysis

### Broker.dispatch

**Current, verified:** `siphonophore_harness/broker.py:32-39`.
```python
class Broker:
    def __init__(self, gate: Gate, executor: Executor) -> None: ...
    def dispatch(self, intent: Intent, authority: Authority | None = None) -> Effect:
        decision = self._gate.submit(intent, authority=authority)
        return self._executor.execute(decision, intent)
```
`Broker` lives in `siphonophore_harness/`, not `siphonophore_core/` — mechanically confirmed
(`find . -name broker.py` returns exactly one file, under `siphonophore_harness/`). **The target
design's characterization of this as harness-owned, not core, is correct as stated; no
correction is required.** `Gate`/`Executor`/`Decision`/`Effect`/`Intent` are core; the *composition*
of them behind one `dispatch()` call is harness code.

**Options re-evaluated (Critical Planning Question 1):**

| Option | Layer | Compatibility | Test blast radius | Abstraction quality | Usefulness to external builders | Solves general info-loss, or REPL convenience only? |
|---|---|---|---|---|---|---|
| A — change `Broker.dispatch()`'s return contract | Harness | Breaks every direct caller (see §6) | 4 test files, ~9 call sites | Good, if the new shape is curated not a grab-bag | High — every future harness reaching through `Broker` gets it | General: any caller of `Broker`, not just the REPL, currently loses `Decision` |
| B — preserve `dispatch()` exactly, capture `Decision` at a harness-level composition seam outside `Broker` | Harness | High (additive) | New tests only | Poor as evaluated: the only seam that exists is calling `Gate.submit()`/`Executor.execute()` directly, which is exactly what `Broker`'s own docstring (`broker.py:1-4`) says defeats the point of `Broker` existing | Low — duplicates `Broker`'s own sequence, risks divergence | REPL convenience with a maintenance cost |
| C — preserve `dispatch()`, expose only a projection through another existing hook | Harness/Core | High | None if no such hook exists | N/A — no existing hook was found that surfaces mid-dispatch state without either widening `dispatch()` or duplicating it | N/A | N/A — collapses into A or B |
| D — reusable harness-level operation/result wrapper, core still returns `Effect` | Harness | Same as A (the wrapper is what `dispatch()` returns) | Same as A | Good | High | General |
| E — A for mechanism + a curated projection (not raw `Decision`) for shape | Harness | Same as A | Same as A | Best — avoids leaking `Decision.token` (a cryptographic value, `mediation.py:39`) into presentation code that has no use for it | High | General |

Option E is what the target design selects (§10). Re-evaluated independently here against the
actual `broker.py` source (not re-derived from the design's prose) and **confirmed as the correct
minimal choice**: Option A alone would tempt returning the raw `Decision`, which is unnecessary
(operator needs `permitted`/`execution_class`/`authority_id`/`order_id`, not `token` or the
already-implicitly-confirmed `artifact_digest`) and mildly risky (a token with no meaning outside
`Gate` reaching presentation code). Option B/C were checked directly against the current file layout
and found to have no seam that doesn't either duplicate `Broker`'s sequence or require the same
`dispatch()` widening anyway.

**Planning conclusion: CONFIRMED AS DESIGNED.** No design correction required for Broker
ownership/contract semantics. The one implementation-shape completion needed (Effect-compatible
result, §3 Refinement 2) is recorded as a Stage 2 requirement, not a design change.

### Exception hierarchy

**Current, verified raise sites** (all re-read directly from `execution.py`/`mediation.py`):

| Site | File:line | Condition | Current type | Reachable via `Broker.dispatch()`? |
|---|---|---|---|---|
| 1 | `execution.py:141-142` | `decision` doesn't correspond to `intent` (id/kind mismatch) | bare `GateViolation` | No — Broker always uses the same Intent it minted with |
| 2 | `execution.py:143-144` | `Gate.verify(decision)` fails (forged/tampered/downgraded) | bare `GateViolation` | No — Broker's own Decision is freshly minted, never touched in between |
| 3 | `execution.py:145-146` | `decision.permitted is False` (ordinary DENY) | bare `GateViolation` | **Yes — the common case** |
| 4 | `execution.py:150-154` | artifact digest mismatch | `ArtifactMismatchError(GateViolation)` — **already distinct** | No, same reasoning as #2 |
| 5 | `execution.py:157-158` | no backend registered for `execution_class` | bare `GateViolation` | **Yes — realistic** (e.g. policy resolves to `uid_cgroup` against a portable-only `Executor`) |
| 6 | `mediation.py:98` | `authority` fails Gate re-verification | bare `GateViolation` | Yes, if a caller passes a bad `Authority` |
| 7 | `mediation.py:100` | `intent.principal_id != authority.principal_id` | bare `GateViolation` | Yes, same condition class as #6 |
| 8 | `mediation.py:185,188,221,223,226` | order/parent-authority verification and scope/depth refusals in `issue_order`/`grant_root_authority`/`delegate` | bare `GateViolation` | N/A — not part of `dispatch()`, part of authority *granting*, never called through `Broker` |
| 9 | `execution.py:33-36` (raised at various sites) | backend/artifact execution failure | `ExecutionError(RuntimeError)` — separate hierarchy, already distinct | Yes |
| 10 | `identity.py:33-36` | check-in identity failure | `IdentityError(RuntimeError)` — separate hierarchy, already distinct | Only if a check-in backend is registered (not true of the REPL today) |
| 11 | `siphonophore_harness/intent_parsing.py:40` | malformed/hostile completion | `IntentParseError(ValueError)` — harness-owned, already distinct | N/A — raised before `Broker.dispatch()` is even called |

**Classification per candidate (Critical Planning Question 2):**

| Candidate | Current type | Distinguishable today without message-parsing? | Classification |
|---|---|---|---|
| Ordinary policy DENY (#3) | bare `GateViolation` | No | **CORE TYPE JUSTIFIED** — happens inside `Executor.execute()`, the same call frame as #2 and #5; a caller (harness or otherwise) cannot distinguish these three by call-site alone the way #6/#7 can be distinguished from #3/#5 (see below). A distinct type is the only mechanism that works here. Realistic, general (any direct `siphonophore_core` consumer hits this today, not just the reference harness — `tests/test_execution.py:90-95` proves it independent of any harness). |
| Decision failed re-verification (#2) | bare `GateViolation` | No | **CORE TYPE JUSTIFIED** — same call-frame argument as above; this is a materially different security condition (the Decision itself is untrustworthy) from an ordinary signed "no," and conflating them was exactly the assessment's original finding (§7/§10). |
| No backend registered (#5) | bare `GateViolation` | No | **CORE TYPE JUSTIFIED** — same call-frame argument; categorically a configuration/environment gap, not an authorization judgment, and realistically reachable (unlike #1/#2/#4 through Broker). |
| Authority/principal-mismatch pre-Decision refusal (#6, #7) | bare `GateViolation` | **Yes, structurally** — these raise from inside `Gate.submit()`, before `Broker.dispatch()`'s own `decision = ...` assignment ever completes; a harness-level `try/except` around the `submit()` call vs. the `execute()` call already distinguishes "no Decision was ever minted" from "a Decision exists and something about dispatching it failed," with no new core type needed. | **HARNESS NORMALIZATION SUFFICIENT** — adding subclasses here would duplicate a distinction the call structure already gives the harness for free. |
| Order/authority-granting refusals (#8) | bare `GateViolation` | Partially (by call-site: `issue_order`/`grant_root_authority`/`delegate` vs. `submit`/`execute`) | **OPEN, OUT OF SCOPE for this arc** — not part of `dispatch()`'s outcome space (§9's eight categories are defined over `Broker.dispatch()` outcomes only); whether these deserve their own subclasses is an independent question about the authority-*granting* API, untouched by the REPL/reference-harness UX work. Recorded as genuinely open, not silently dropped. |
| Decision-does-not-correspond-to-intent (#1) | bare `GateViolation` | No | **PRESENTATION ONLY / OUT OF SCOPE** — unreachable via `Broker.dispatch()` (§3 Refinement 1); not part of the outcome-category set; no test exercises it today. |
| Artifact digest mismatch (#4) | `ArtifactMismatchError` | Already yes | **Already CORE TYPE JUSTIFIED** — no new work. |
| Check-in identity failure | `IdentityError` | Already yes (separate hierarchy) | **Already CORE TYPE JUSTIFIED** — no new work. |
| Backend/artifact execution failure | `ExecutionError` | Already yes (separate hierarchy) | **Already CORE TYPE JUSTIFIED** — no new work. |
| Malformed/hostile completion | `IntentParseError` | Already yes (harness-owned) | **Already CORE TYPE JUSTIFIED at its own layer** — no core change; not a `siphonophore_core` type at all. |

**Why exactly three new subclasses, not more or fewer:** the deciding test is not "is this
semantically distinct" (all eleven rows above are) but "is this distinction inaccessible by any
means other than a new type." Rows #6/#7 are already accessible by call-site; row #8 is out of
`dispatch()`'s scope entirely; row #1 is unreachable via the sanctioned path. Only #3, #2, and #5
share both properties — semantically distinct *and* structurally inaccessible except by type — which
is exactly the position `ArtifactMismatchError` (#4) already occupies and the precedent it set. This
independently reproduces the target design's own selection (§10/§15: "Three narrow, additive
subclasses... paralleling `ArtifactMismatchError`") from first principles against current code,
rather than accepting the count without re-deriving it.

**Planning conclusion: CONFIRMED AS DESIGNED.** Three new subclasses, at exactly sites #2, #3, #5,
each `GateViolation` subclasses (backward-compatible with every existing `except GateViolation`/
`pytest.raises(GateViolation)` site — 20 such assertions found across
`tests/{test_harness_broker,test_authority,test_harness_loop_k8s_cluster,test_harness_loop,
test_harness_loop_linux,test_execution}.py`, none of which need to change since a subclass instance
still satisfies `isinstance(x, GateViolation)`). Exact names deferred to Stage 1 (matching the
target design's own open question 4) but must follow `ArtifactMismatchError`'s naming precedent
(a `*Error` suffix, docstring stating the distinct semantic condition and citing the exact raise
site it replaces).

### CognitiveLoop

**Current, verified:** `siphonophore_harness/loop.py:35-70`. `__init__` accepts exactly `{model,
broker, principal_id, authority}` (mechanically enforced,
`tests/test_harness_structural_proof.py:52-56`). `step()` returns exactly what
`self._broker.dispatch(...)` returns, unmodified (`loop.py:67,70`). Nothing in this plan proposes
widening `__init__`'s accepted parameters. `step()`'s *return value* changes only insofar as
`Broker.dispatch()`'s does (§3 Refinement 2) — no new logic is added to `CognitiveLoop` itself if
the new result type is Effect-compatible by delegation, per that refinement's conclusion.

### Outcome representation

**Target representation (Critical Planning Question 3):** re-evaluated the five stated options
directly against current `Broker`/`Effect`/`Decision`/exception-flow source, not merely accepting
the design's own framing:

| Option | Verdict |
|---|---|
| A — reuse existing objects directly (e.g. return `(Effect, Decision)`) | Rejected — leaks raw `Decision.token`; no field curation |
| B — small harness-owned result/dataclass | **Selected** — matches Option E of the Decision-visibility analysis above |
| C — tuple/composition of existing objects | Rejected, same reason as A |
| D — normalized exception + `Effect` only (no Decision-derived data) | Rejected — insufficient: omits `authority_id`/`order_id`/`permitted`, failing UX requirement 9 (§6 of the design) |
| E — other | Not needed; B is sufficient |

**Responsible layer:** `siphonophore_harness/` (a new module, e.g. an `outcome.py` alongside
`broker.py`/`loop.py` — exact module name and field names are Stage 2 implementation details, not
architecture, matching the target design's own explicit deferral, §18 open question 1). **Not
core** — nothing about classifying already-existing core exception types into a closed category set,
or curating which `Decision` fields to expose, requires a `siphonophore_core` change.

**Compatibility impact:** see §6.

### Backend/profile composition

**Current, verified mechanism:** `Executor.__init__(gate, backends=None)` defaults to
`{same_process, separate_process}` if `backends` omitted (`execution.py:130-135`);
`register_backend()` adds one more after construction (`execution.py:137-138`).
`ConsequencePolicy.__init__(mapping=None, allowed_kinds=None)` accepts an arbitrary
`consequence -> execution_class` mapping (`policy.py:76-82`). Confirmed by direct construction in
`tests/test_harness_loop_k8s_cluster.py:123-128` (`ConsequencePolicy(mapping={"k8s": "k8s_pod"})`,
`Executor(gate, backends={"k8s_pod": counting})`) and `tests/test_harness_loop_linux.py` (uid_cgroup
wiring) — both build a complete alternate substrate with zero core changes, exactly as the
assessment and design both claim. No registry, plugin system, or discovery mechanism exists at the
core level, and none is needed.

**Planning conclusion:** no core change. The reusable composition layer (Stage 3) is a thin,
additive convenience over these two constructors — a way to name and discover profiles that are
already fully composable today.

## 6. Compatibility analysis

| Symbol | Current signature/behavior | Proposed change | Internal callers needing update | External-looking callers | Backward compatible? | Migration needed |
|---|---|---|---|---|---|---|
| `Broker.dispatch(intent, authority=None) -> Effect` | Returns bare `Effect`; raises on refusal | Returns a small, Effect-compatible result carrying `Effect` + a curated Decision projection; attaches the same projection to raised exceptions where a `Decision` was minted | `CognitiveLoop.step()` (`loop.py:67,70`) — no code change needed if the result is Effect-compatible by delegation (§3 Refinement 2) | `tests/test_harness_broker.py` (5 dispatch sites), `tests/test_harness_loop_k8s_cluster.py` (2), `tests/test_harness_loop_linux.py` (3), `tests/test_harness_loop.py` (via `CognitiveLoop.step()`, indirect); `experiments/k8s_agentwatch_observation/*.py` and `experiments/k8s_mediation_bypass/*.py` (not covered by `testpaths = ["tests"]`, so not caught by CI, and explicitly out of scope to edit in this arc) | **Source-compatible for attribute access** if the new type delegates `.intent_id`/`.execution_class`/`.detail` to the wrapped `Effect` (recommended); **not compatible** for any code doing `isinstance(x, Effect)` or a strict type check on the return value (none found in the current codebase, including `experiments/`) | Update the ~10 direct-assertion test call sites listed; no shim needed if delegation is used |
| `Executor.execute(decision, intent) -> Effect` | Raises bare `GateViolation` at 3 sites (#2,#3,#5 above) | Same conditions, same call order, only the exception *type* changes at those 3 sites (subclasses of `GateViolation`) | None — internal to `execution.py` | `tests/test_execution.py:79-103` (3 tests, currently `pytest.raises(GateViolation)`) | **Fully backward compatible** — subclass instances satisfy every existing `except GateViolation`/`pytest.raises(GateViolation)` | Optional: tighten `test_execution.py`'s 3 assertions to the new specific subclasses; not required for correctness |
| `GateViolation` hierarchy | One base + `ArtifactMismatchError` | +3 new subclasses at sites #2/#3/#5 | None | Same 20 existing `pytest.raises(GateViolation)`/`except GateViolation` sites across 6 test files — all continue to pass | Fully compatible | None required |
| `Effect`/`Decision`/`Intent` dataclasses | Unchanged fields | No change | — | — | Fully compatible | None |
| Backend registration (`Executor(gate, backends=...)`, `ConsequencePolicy(mapping=...)`) | Already sufficient | No change; only a new convenience wrapper added alongside | `examples/repl.py` (gains a flag using the new convenience) | None | Fully compatible | None |

## 7. Target file/component map

| File/component | Classification | Why |
|---|---|---|
| `siphonophore_core/execution.py` | **WILL CHANGE** | 3 new `GateViolation` subclasses at existing raise sites (Stage 1) |
| `siphonophore_core/mediation.py` | **SHOULD NOT CHANGE** | Authority-related raise sites (#6-#8) classified HARNESS NORMALIZATION SUFFICIENT / OUT OF SCOPE |
| `siphonophore_core/policy.py`, `intent.py`, `authority.py`, `identity.py`, `audit.py` | **SHOULD NOT CHANGE** | No gap found requiring a change; `audit.py` confirmed to have no coupling to `Effect`/`Decision`/`Broker` at all |
| `siphonophore_core/execution_k8s.py`, `execution_uid_cgroup*.py`, `execution_spawn_helper*.py` | **SHOULD NOT CHANGE** | Already-sufficient substrate implementations; nothing in this plan touches backend internals |
| `siphonophore_harness/broker.py` | **WILL CHANGE** | Widened return/attach contract (Stage 2) |
| `siphonophore_harness/loop.py` | **MAY CHANGE** | Only if the Stage 2 result type is not fully Effect-compatible by delegation; otherwise unchanged (§3 Refinement 2) |
| `siphonophore_harness/__init__.py` | **MAY CHANGE** | Re-export additions for the new outcome/wiring modules, if Stage 2/3 choose to add them there |
| new module (outcome classification, exact name TBD Stage 2) | **WILL ADD** | New, additive |
| new module or extension (named substrate/profile wiring, exact shape TBD Stage 3) | **WILL ADD** | New, additive |
| `siphonophore_harness/model.py`, `model_anthropic.py`, `intent_parsing.py`, `prompts.py` | **SHOULD NOT CHANGE** | No finding in either the assessment or this plan touches these |
| `examples/repl.py` | **WILL CHANGE** | Rendering rewrite (Stage 4), substrate/authority flags (Stage 3/5 consumption) |
| `tests/test_execution.py` | **MAY CHANGE** | Optional tightening of 3 assertions to new subclasses |
| `tests/test_harness_broker.py`, `test_harness_loop.py`, `test_harness_loop_linux.py`, `test_harness_loop_k8s_cluster.py` | **WILL CHANGE** | Direct-assertion updates for the widened `Broker.dispatch()` return (Stage 2); their *substrate/authority logic itself* does not change |
| `tests/test_mediation.py`, `test_authority.py`, `test_identity.py`, `test_identity_linux.py`, `test_audit.py`, `test_audit_linux.py`, `test_policy.py`, `test_intent.py`, `test_core_no_k8s_vocabulary.py`, `test_harness_structural_proof.py`, all `test_execution_*` files other than `test_execution.py` | **SHOULD NOT CHANGE** | Nothing in this plan touches the mechanisms they cover |
| `experiments/k8s_agentwatch_observation/*.py`, `experiments/k8s_mediation_bypass/*.py` | **SHOULD NOT CHANGE (explicitly out of scope to edit)**, but flagged as a **known, disclosed compatibility risk** | Call `Broker.dispatch()` and assume a bare `Effect` return; not covered by `testpaths`; mitigated, not fixed, by making the new result type Effect-compatible by delegation |
| `docs/REFERENCE_HARNESS_ASSESSMENT.md`, `README.md`, `DESIGN.md`, `docs/EXECUTION*.md` | **SHOULD NOT CHANGE** | Out of scope for this stage; README's own "Not yet implemented" section will need a future, separate update once implementation lands |

## 8. Test strategy

**Existing tests expected to remain green throughout, unmodified:** all of `test_mediation.py`,
`test_authority.py`, `test_identity.py`, `test_identity_linux.py`, `test_audit.py`,
`test_audit_linux.py`, `test_policy.py`, `test_intent.py`, `test_core_no_k8s_vocabulary.py`,
`test_harness_structural_proof.py`, `test_harness_intent_parsing.py`, `test_harness_model*.py`,
`test_harness_prompts.py`, and every `test_execution_*` file other than `test_execution.py` itself
(`test_execution_k8s.py`, `test_execution_k8s_cluster.py`, `test_execution_root_refusal*.py`,
`test_execution_spawn_helper_linux.py`, `test_execution_uid_cgroup*.py`,
`test_execution_uid_cgroup_env_linux.py`, `test_provision_cgroup_execution_id.py`,
`test_spawn_helper_linux.py`, `test_default_child_env.py`, `test_elevation_prefix.py`).

**Existing tests to extend:** `test_execution.py` (optionally tighten 3 assertions to new
subclasses, Stage 1); `test_harness_broker.py`, `test_harness_loop.py`, `test_harness_loop_linux.py`,
`test_harness_loop_k8s_cluster.py` (update ~10 call sites to the widened return shape, Stage 2; their
own substrate/authority assertions are untouched per the design's own §16.7 acceptance criterion).

**New tests required, by stage:**
- Stage 1: one test per new subclass proving it (not the bare base) is raised at its specific site;
  a test proving the existing 20 `pytest.raises(GateViolation)`/`except GateViolation` sites still
  pass unmodified.
- Stage 2: a test per outcome category proving the classifier maps the right core
  type/condition to the right category (all 8 from §9 of the design); a test proving the projection
  excludes `token` and `artifact_digest`; a test proving `CognitiveLoop.step()` (not only direct
  `Broker.dispatch()` calls) yields the enriched result; a test proving `authority_rejected` (raised
  before a `Decision` exists) and `denied`/`integrity_rejected`/`backend_unavailable` (raised with a
  `Decision` already minted) are distinguishable via the classifier without string-matching.
- Stage 3: a test asserting the "portable" named profile registers exactly `{same_process,
  separate_process}`; a test asserting the discoverability accessor reports the correct registered
  classes and active mapping for an arbitrary `Executor`/`Policy` pair.
- Stage 4: no new automated test is required if `examples/repl.py` remains untested by convention
  (confirmed: no `test_repl.py` exists today) — acceptance is a manual walkthrough (see Stage 4).
- Stage 5: none beyond what Stage 2/existing `test_harness_loop_linux.py` already prove.

**Negative/adversarial cases, by stage:** see each stage's own section below and §11.

## 9. Stage plan

### Stage 1 — Core exception subclasses

**Objective:** Add three new, narrow, additive `GateViolation` subclasses in
`siphonophore_core/execution.py`, at raise sites #2 (`execution.py:143-144`, Decision failed
re-verification), #3 (`execution.py:145-146`, ordinary policy DENY), and #5 (`execution.py:157-158`,
no backend registered), following `ArtifactMismatchError`'s existing precedent (subclass name,
docstring stating the exact distinct condition and citing the raise site it replaces). No change to
condition logic, check order, or raised messages beyond what the new `__init__`/docstring requires.

**Why this stage exists:** §5's exception-hierarchy analysis found these three, and only these
three, both semantically distinct and structurally inaccessible except by type — the same position
`ArtifactMismatchError` already occupies. This closes the one core-level gap independently justified
by direct-`siphonophore_core`-consumer needs (`tests/test_execution.py` already proves the gap
exists with zero harness code involved), not by REPL convenience.

**Environment:** `sipho-integration` (portable, no cluster, no root).

**Files likely changed:** `siphonophore_core/execution.py` (WILL CHANGE); `tests/test_execution.py`
(MAY CHANGE, optional tightening).

**Must not change:** `Decision`/`Effect`/`Intent` field shapes; `Gate`/`Executor` control flow or
check order; `mediation.py` (raise sites #6-#8 stay bare `GateViolation`, per §5's classification);
any test file other than `test_execution.py`.

**Tests:** new tests asserting each of the three new subclasses is raised at its specific site (not
the base class); full portable suite re-run to confirm the existing 20
`pytest.raises(GateViolation)`/`except GateViolation` sites across 6 files still pass unmodified.

**Adversarial/negative checks:** a subclass raised at the wrong site (e.g. the DENY subclass raised
for a no-backend condition) would be a correctness bug — assert the exact subclass per site, not
just "a `GateViolation` subclass"; re-run `test_core_no_k8s_vocabulary.py` to confirm the new classes
introduce no Kubernetes vocabulary; re-run `test_harness_structural_proof.py` to confirm no import
or signature change leaked into harness-facing files.

**Acceptance criteria:** portable suite green at 144 + N new (N = number of new tests added); three
named subclasses exist, one per site, each a `GateViolation` subclass with a docstring stating its
distinct condition; zero existing test modified except optionally `test_execution.py`.

**Stop conditions:** if adding a subclass would require reordering or changing the semantics of the
existing checks in `Executor.execute()` — stop and report; the checks' order and meaning are
load-bearing (mediation-invariant related) and were not found to need any change.

**Commit boundary:** one commit — `feat(core): add narrow GateViolation subclasses for denied,
decision-verification-failed, and no-backend-registered outcomes`.

**Dependencies on later stages:** Stage 2's outcome classifier depends on these three types existing
to distinguish `denied`/`integrity_rejected`/`backend_unavailable` without message-parsing.

---

### Stage 2 — Reusable harness composition / outcome representation

**Objective:** In `siphonophore_harness/`, add a small, additive, Effect-compatible result type
that `Broker.dispatch()` returns on success (wrapping the `Effect` plus a curated `Decision`
projection — `permitted`, `execution_class`, `authority_id`, `order_id`; excluding `token` and
`artifact_digest`) and attaches (via an attribute) to raised exceptions on refusal wherever a
`Decision` was actually minted before the refusal (every category below except `input_rejected` and
`authority_rejected`). Add a pure classifier function, defined entirely over
`siphonophore_core` types (the 3 new Stage 1 subclasses, `ArtifactMismatchError`, `IdentityError`,
`ExecutionError`, `IntentParseError`, and success), mapping a `Broker.dispatch()` outcome to exactly
one of the eight named categories from the target design's §9
(`executed`/`input_rejected`/`authority_rejected`/`denied`/`integrity_rejected`/
`backend_unavailable`/`identity_rejected`/`execution_failed`). Ensure `CognitiveLoop.step()`
requires no logic change to propagate the enriched result (§3 Refinement 2) by making the new
result type delegate `.intent_id`/`.execution_class`/`.detail` attribute access to the wrapped
`Effect`.

**Why this stage exists:** this is the single highest-leverage item in both the assessment (§16.1)
and the design (§10) — closes the Decision/Effect visibility gap and the machine-consumability
requirement (design §6, requirement 10) in one additive, harness-layer change with zero core impact.

**Environment:** `sipho-integration`, portable only.

**Files likely changed:** `siphonophore_harness/broker.py` (WILL CHANGE); new module for the result
type/classifier, e.g. `siphonophore_harness/outcome.py` (WILL ADD, exact name a Stage 2
implementation decision); `siphonophore_harness/loop.py` (MAY CHANGE — only if delegation alone
doesn't suffice); `siphonophore_harness/__init__.py` (MAY CHANGE, re-exports);
`tests/test_harness_broker.py`, `test_harness_loop.py`, `test_harness_loop_linux.py`,
`test_harness_loop_k8s_cluster.py` (WILL CHANGE, return-shape assertions only).

**Must not change:** `Gate`/`Executor`/mediation semantics; `Decision`/`Effect` field shapes; the
structural non-bypass proof's enforced `CognitiveLoop.__init__` signature; the substrate/authority
*logic* asserted by the four test files being updated — only their assertions against the changed
return shape.

**Tests:** per §8 above — one test per outcome category, a `token`/`artifact_digest`-exclusion test,
a `CognitiveLoop.step()`-propagation test, and an `authority_rejected`-vs-`denied` distinguishability
test.

**Adversarial/negative checks:**
- Prove the projection never contains `Decision.token` under any code path (a leaked bearer/
  cryptographic value reaching presentation code would be a real vulnerability, not merely an API
  wart).
- Prove `k8s_cluster`- and `linux_root_only`-marked tests' own substrate/authority assertions are
  unchanged in *meaning* — only their access pattern against the new return shape changes (matches
  design §16.7 verbatim).
- Prove a caller relying on duck-typed `Effect` attribute access (`.intent_id`, `.execution_class`,
  `.detail`) — which includes the out-of-scope `experiments/` consumers — continues to work
  unmodified against the new result type, even though those files are not edited in this arc.
- Prove `denied` and `integrity_rejected` remain distinguishable from each other (both now have
  distinct Stage 1 types) and from `authority_rejected` (distinguishable by call-site/no-Decision,
  per §5) without any message-string comparison anywhere in the classifier.

**Acceptance criteria:** portable suite green; the 4 updated test files' substrate/authority
assertions unchanged in meaning; the classifier is a pure function of existing
`siphonophore_core` types with a closed, documented 8-category set; `CognitiveLoop.step()`'s own
return value carries the enriched result (verified by test, not merely by `Broker.dispatch()`'s
own).

**Stop conditions:** if achieving Effect-compatibility requires any change to `Effect` itself
(core) — stop and report; this would violate "no core changes for REPL/harness convenience" and was
not found to be necessary (delegation from a harness-owned wrapper class is sufficient).

**Commit boundary:** one or two commits — `feat(harness): widen Broker.dispatch() to carry a
Decision projection` and, if separated, `feat(harness): reusable dispatch outcome-category
classifier`.

**Dependencies:** requires Stage 1 complete (classifier needs the 3 new subclasses). Stage 3 is
independent and may proceed in parallel or either order relative to this stage. Stage 4 depends on
this stage.

---

### Stage 3 — Substrate/profile composition exposure

**Objective:** Add a documented, reusable "named wiring" convenience in `siphonophore_harness/`
(e.g. a function returning a ready `{Gate, Executor, Broker}` triple for a named profile — at
minimum `"portable"`, today's default `{same_process, separate_process}`) and a discoverability
accessor that reports an `Executor`'s currently-registered execution classes and a
`ConsequencePolicy`'s active mapping, callable without inspecting internals. Do not choose exact
`examples/repl.py` flag syntax here (deferred to Stage 4) beyond confirming the convenience is
callable with the information a flag would supply.

**Why this stage exists:** closes assessment finding §16 open-question 2 / §11 of the design — the
one piece of duplication (`examples/repl.py:62-65`'s inline construction) a second front end would
otherwise have to copy by hand, and the substrate-discoverability requirement (design §6,
requirement 2).

**Environment:** `sipho-integration` for the wiring/discoverability code and its portable tests
(constructing wirings and asserting on registered names requires no live cluster or root). `agent-vm`
is used only if Stage 7 is later determined necessary (§10).

**Files likely changed:** new module or `siphonophore_harness/__init__.py` extension (WILL ADD,
exact placement a Stage 3 decision per the design's own open question 2); `examples/repl.py`
(untouched in this stage — flag consumption is Stage 4).

**Must not change:** `Executor`/`ConsequencePolicy`/`ExecutionBackend` core constructors; no
Kubernetes-specific branching in the wiring helper's own vocabulary beyond existing execution-class
strings (`"k8s_pod"`, etc., which already exist in core policy vocabulary today).

**Tests:** a test asserting the `"portable"` named profile registers exactly `{same_process,
separate_process}`; a test asserting the discoverability accessor correctly reports an arbitrary
`Executor`/`Policy` pair's registered classes/mapping. No live-cluster test in this stage.

**Adversarial/negative checks:** re-run `test_core_no_k8s_vocabulary.py` (unaffected, since this
stage touches only `siphonophore_harness/`) and manually confirm the new module itself introduces no
`Pod`/`Namespace`/`ServiceAccount`-shaped field or parameter names; confirm selecting a
`"k8s_pod"`-named profile without a reachable cluster fails clearly at dispatch time (the existing
`K8sPodBackend`'s own behavior, unchanged) rather than silently substituting a different backend.

**Acceptance criteria:** an operator/front-end can learn available execution classes and select a
named profile at construction time using only `siphonophore_harness` public functions; zero core
changes.

**Commit boundary:** one commit — `feat(harness): named substrate-profile wiring and
discoverability`.

**Dependencies:** none on Stage 1/2. Stage 4 depends on this stage.

---

### Stage 4 — Reference REPL / operator presentation

**Objective:** Rewrite `examples/repl.py`'s rendering only (no new mediation logic) to: (a) print
`execution_class` and `intent_id` by default every turn, no longer gated behind `--verbose`
(`execution_class`) or never printed (`intent_id`); (b) replace the current three collapsing
`except` clauses (`examples/repl.py:84-92`) with rendering driven by the Stage 2 outcome-category
classifier — one distinct, plain-language label per category, not exception class names or raw
messages; (c) print a one-time session-start line disclosing registered execution classes and active
consequence mapping, via Stage 3's discoverability accessor; (d) add a flag selecting a named
substrate profile (Stage 3); (e) add a way to construct the session with a pre-supplied `Authority`
and, when one is held, display `authority_id` and scope — hidden entirely for the ordinary
authority-less path.

**Why this stage exists:** this is the actual operator-facing deliverable every prior stage exists
to make possible — the assessment's central finding (§3-§5) is that this presentation layer, not the
underlying mechanism, is the deficiency.

**Environment:** `sipho-integration`.

**Files likely changed:** `examples/repl.py` (WILL CHANGE, extensively).

**Must not change:** anything in `siphonophore_core`/`siphonophore_harness`'s semantic layer —
this stage only reads and prints what Stages 2/3 already expose.

**Tests:** no `test_repl.py` exists today (confirmed) and this plan does not require inventing one
as a hard gate — acceptance for this specific stage is a manual, scripted-model or real-model
walkthrough (see Acceptance criteria). If the implementer finds it easy to factor the new rendering
logic into a separately testable function during Stage 4, doing so is encouraged but not mandated by
this plan.

**Adversarial/negative checks:** manually drive at least one reachable case per category
(`executed`, `denied`, `backend_unavailable`, `input_rejected` are reachable without root/cluster;
`integrity_rejected` reachable by constructing a swapped-artifact scenario akin to
`test_execution.py:64-76`; `authority_rejected` reachable by supplying a forged/mismatched
`Authority`) and confirm each renders a visibly distinct label; confirm the authority section is
absent when no `Authority` was supplied (regression check against today's default); confirm
`--verbose`'s existing raw-completion/detail output is additive, not replaced.

**Acceptance criteria:** a manual REPL walkthrough demonstrates default-visible
`execution_class`+`intent_id`; distinct plain-text labels for every category reachable without live
Kubernetes/root; the substrate-discoverability line at startup; authority display only when
constructed with one.

**Commit boundary:** one commit — `feat(examples): reference REPL renders structured dispatch
outcomes`.

**Dependencies:** requires Stage 2 and Stage 3 complete.

---

### Stage 5 — Authority/delegation presentation & documentation (minimum bar)

**Objective:** Document a recipe (script or README-adjacent snippet, not new orchestration code)
showing how to construct and supply a pre-built `Authority` to the reference harness at construction
time, mirroring `tests/test_harness_loop_linux.py:278-321`'s existing pattern; confirm Stage 4's
authority-display wiring (item (e)) is complete and sufficient; explicitly do not add any code
deciding *when* to delegate or constructing a second `CognitiveLoop` automatically.

**Why this stage exists:** closes the "documented advanced path" requirement from design §12 without
building orchestration — matches the design's own explicit non-goal.

**Environment:** `sipho-integration`.

**Files likely changed:** `examples/repl.py` (MAY CHANGE, small, if Stage 4 did not already fully
cover item (e)); a documentation artifact for the recipe — note `README.md` is outside this
*planning* stage's allowed-file list and any doc update belongs to whichever implementation stage
actually needs it, confirmed with the user/reviewer at that time rather than assumed here.

**Must not change:** no orchestration component; no new `Gate`/`Authority` semantics.

**Tests:** none beyond what Stage 2 and the existing `test_harness_loop_linux.py` already prove;
this stage is presentation/documentation only.

**Adversarial/negative checks:** confirm the recipe cannot be mistaken for an automatic
orchestrator — it must require the operator to run `Gate.issue_order`/`grant_root_authority`/
`delegate` themselves, exactly as test code does today.

**Acceptance criteria:** a documented, working recipe exists; REPL displays authority context when
constructed with one; no orchestration code added anywhere.

**Stop conditions:** if implementation pressure starts adding "decide when to delegate" logic —
stop; that is explicitly out of scope for this entire arc.

**Commit boundary:** one commit — `docs(examples): document supplying a pre-constructed Authority to
the reference harness`.

**Dependencies:** Stage 4.

---

### Stage 6 — Portable integration/regression validation

**Objective:** Full re-run of the portable suite plus the two structural/no-k8s-vocabulary
invariant tests, plus a manual end-to-end REPL smoke session, confirming no regression across Stages
1-5 combined.

**Environment:** `sipho-integration`.

**Files likely changed:** none (validation only).

**Tests:** full portable suite (`pytest -q -m "not k8s_cluster and not linux_root_only"`), expected
count = 144 + every new test added across Stages 1-3 (exact final number recorded when this stage
runs, not predicted here); `test_harness_structural_proof.py` and `test_core_no_k8s_vocabulary.py`
explicitly re-verified green.

**Adversarial/negative checks:** re-run the full, uncollapsed suite (not just the changed files) to
catch any cross-file interaction Stages 1-5 individually might have missed.

**Acceptance criteria:** matches target design §16 items 6-7 verbatim: structural/no-k8s-vocabulary
invariants hold; full portable suite passes with no regression in `k8s_cluster`/`linux_root_only`
tests' own logic.

**Commit boundary:** none required if all prior stages' own commits already leave the tree green;
otherwise a small fixup commit.

**Dependencies:** Stages 1-5.

---

### Stage 7 — Real Kubernetes validation (agent-vm), conditional

**Objective:** Only if Stage 3 introduces a `"k8s_pod"`-named built-in profile that
`examples/repl.py` or `siphonophore_harness/` itself can select (as opposed to leaving `k8s_pod`
registration to the operator via existing public constructors, documented but not one of the
wiring helper's named profiles) — validate that reference-harness-level selection against a real
`kind` cluster, on top of what `tests/test_harness_loop_k8s_cluster.py` already proves at the core
level.

**Environment:** `agent-vm`, only for this stage.

**Files likely changed:** none new — validates Stage 3/4 output already committed.

**Stop conditions:** if Stage 3 does not add a built-in `"k8s_pod"` named profile, this stage does
not run at all — do not use `agent-vm` merely because `k8s_pod`/`K8sPodBackend` exist in core; they
are already validated by `tests/test_harness_loop_k8s_cluster.py`.

**Dependencies:** Stage 3 (and, transitively, Stage 4 if the profile is only reachable via a REPL
flag).

## 10. Kubernetes validation boundary

`agent-vm` is used **only** for Stage 7, and only if Stage 3 concretely adds a built-in,
harness-named `"k8s_pod"` profile that the reference harness itself selects — not merely because
`k8s_pod`/`K8sPodBackend` exist in `siphonophore_core` (they do today, already validated by
`tests/test_harness_loop_k8s_cluster.py`, which requires no changes in this arc). Every other stage,
including all of the exception, outcome-representation, and portable-substrate-wiring work, runs
entirely on `sipho-integration` with no cluster dependency.

## 11. Deferred/open questions

1. **Exact names of the three new `GateViolation` subclasses, and exact field names/module location
   of the Stage 2 result type and classifier.** NON-BLOCKING — explicitly deferred to Stage 1/2
   implementation, matching the target design's own open questions 1 and 4.
2. **Exact `examples/repl.py` flag syntax for substrate/authority selection.** NON-BLOCKING —
   implementation detail, Stage 3/4.
3. **Whether/when `README.md`'s "Not yet implemented" section and `docs/EXECUTION*.md` get updated
   to reflect the completed work.** NON-BLOCKING — out of scope for every stage in this plan; a
   separate, later documentation pass once implementation lands.
4. **Whether the order/authority-granting bare-`GateViolation` sites (#8, `mediation.py:185-226`)
   ever deserve their own subclasses.** NON-BLOCKING, genuinely open — independent of this arc's
   `dispatch()`-outcome scope; not addressed by any stage above.
5. **Whether the decision-does-not-correspond-to-intent site (#1, `execution.py:141-142`) should
   eventually get a fourth subclass for completeness, given it is unreachable via `Broker` today.**
   NON-BLOCKING — recorded in §3 Refinement 1; no stage above changes it.

No open question above blocks Stage 1 from starting.

## 12. Definition of done for the whole arc

- All items in target design §16 (minimum acceptance criteria 1-7) are met.
- Three new `GateViolation` subclasses exist, each independently justified per §5's classification.
- `Broker.dispatch()` returns an Effect-compatible, curated structure; `CognitiveLoop.step()`
  requires no separate widening to propagate it.
- A documented, reusable named-wiring convenience and discoverability accessor exist in
  `siphonophore_harness/`, requiring zero core changes.
- `examples/repl.py` renders `execution_class`/`intent_id` by default, distinguishes all
  non-root/non-cluster-dependent outcome categories with plain labels, discloses registered
  substrates at startup, and displays authority context only when held.
- A documented recipe exists for supplying a pre-constructed `Authority`; no orchestration code was
  added anywhere.
- Portable suite green (144 baseline + new tests, exact count recorded at Stage 6); structural
  non-bypass and no-k8s-vocabulary invariants hold unmodified in spirit.
- `agent-vm`/live Kubernetes touched only if Stage 7's condition was actually met.
- No `~/research` access occurred at any stage.
