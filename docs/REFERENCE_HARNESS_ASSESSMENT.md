# Reference Harness UX and Foundation Assessment

**Status: EXPLORATORY ASSESSMENT — NO DESIGN DECISIONS.**

This document inventories the current state of the reference harness (`examples/repl.py` +
`siphonophore_harness/`) as an operator surface and as a composition example. It contains
classifications and requirement-level statements, not a target design and not implementation
guidance. No mechanism is chosen here. Every claim below is sourced to a specific file, line
range, or test.

## 1. Purpose and scope

Siphonophore is a platform-independent execution-security SDK (`siphonophore_core/`). The
reference harness (`siphonophore_harness/` + `examples/repl.py`) is one consumer of that SDK, not
the SDK itself, and not a product. DESIGN.md §6 states the reference harness's usability is "a
first-class engineering concern of this project... not scaffolding kept alive only to run tests"
(DESIGN.md:216-217), and README.md's own "Project status and current direction" names improving it
as active priority 4 (README.md:222-223). This assessment answers one question: what is the actual
delta between today's reference harness and one that is minimally acceptable to operate and
structurally credible as a foundation for other harnesses — split into an operator-UX dimension and
a foundation/composition-quality dimension, per the two independent axes requested.

Explicitly out of scope for this document: choosing a UX mechanism, redesigning any class, running
Kubernetes, and any material drawn from outside this repository.

## 2. Current architecture

```
operator (human, via terminal)
        |
        v
examples/repl.py            <- presentation: argparse, input() loop, print()
        |  constructs
        v
CognitiveLoop (harness)      <- prompt -> completion -> parse_intent -> dispatch -> history
        |  holds: Model, Broker, principal_id, optional Authority
        v
Broker.dispatch(intent, authority=None)   <- harness, single capability: Intent -> Effect
        |
        +--> Gate.submit(intent, authority) --> Decision   (core/mediation.py)
        |         (independent re-verification of authority, principal match, scope; policy)
        |
        +--> Executor.execute(decision, intent) --> Effect  (core/execution.py)
                  (re-verify Decision HMAC, re-check artifact digest, dispatch to backend)
                        |
                        v
              ExecutionBackend  <-- substrate boundary (DESIGN.md §10)
              same_process | separate_process | uid_cgroup | uid_cgroup_checkin | k8s_pod
```

Layering actually enforced in code, not merely documented:
- `siphonophore_core` imports nothing from `siphonophore_harness` (verified: `grep -rn
  siphonophore_harness siphonophore_core/` returns no hits).
- `siphonophore_harness/{loop,intent_parsing,model,broker}.py` import none of a blocklisted set of
  effect-producing stdlib modules (`os`, `subprocess`, `socket`, `sys`, ...), enforced by
  `tests/test_harness_structural_proof.py:16-38`.
- `CognitiveLoop.__init__` accepts exactly `{model, broker, principal_id, authority}` — enforced by
  the same file, `tests/test_harness_structural_proof.py:41-56`.
- Kubernetes vocabulary (`Pod`, `Namespace`, `kubectl`, field/parameter names like `namespace`,
  `pod_id`, ...) is banned from every core file except `execution_k8s.py`, enforced by AST + regex
  scan in `tests/test_core_no_k8s_vocabulary.py`.

These three structural tests are load-bearing evidence, not prose claims — see §4 and §6.

## 3. Current operator path

Traced from `examples/repl.py` (108 lines) as the sole interactive entry point.

**Startup.** `python3 examples/repl.py --model <id>` (`examples/repl.py:49-65`). Requires: the
`anthropic` extra installed, `ANTHROPIC_API_KEY` set or `--api-key` passed, and an exact,
currently-valid Anthropic model id — none hardcoded, none discoverable from the tool itself
(`examples/repl.py:16-19,39-46,51,57-60`). `--principal-id` defaults to `"human-operator"`
(`examples/repl.py:53`). No other flags exist — no way to select a backend, a policy mapping, or an
Authority from the command line.

**Construction.** Exactly one wiring is possible: `Gate(ConsequencePolicy())`,
`Broker(gate=gate, executor=Executor(gate))`, `CognitiveLoop(model=..., broker=..., principal_id=...)`
(`examples/repl.py:62-65`). `Executor(gate)` with no `backends=` argument registers only
`same_process` and `separate_process` (`execution.py:130-135`); `ConsequencePolicy()` with no
`mapping=` argument uses the default `low/high/privileged -> same_process/separate_process/
uid_cgroup` mapping (`policy.py:65-69`). `authority=` is never passed, so `CognitiveLoop` always
runs the authority-less path (`loop.py:36-40`).

**One turn.** User types a message; `loop.step()` appends it to history, calls
`model.complete(history)` (a real Anthropic API call), parses the completion into an `Intent` via
`parse_intent()`, dispatches it through `Broker.dispatch()`, appends the model's raw completion and
a one-line string description of the `Effect` back into history, and returns the `Effect`
(`loop.py:44-70`, `_describe_effect` at `loop.py:73-74`). The REPL prints `loop.last_message` (the
model's own conversational text, if any) every turn (`examples/repl.py:94-97`); with `--verbose`,
it additionally prints the raw completion and `f"execution_class={effect.execution_class!r}
detail={effect.detail}"` (`examples/repl.py:99-102`).

**What the operator can see, by default (no `--verbose`):** the model's conversational text only.
Nothing about `execution_class`, nothing about the `Decision`, nothing about `intent_id`, unless an
error occurs.

**What the operator can see, with `--verbose`:** the raw model completion (JSON), `execution_class`,
and the backend-specific `detail` dict. Still nothing about the `Decision` object itself
(`permitted`, `authority_id`, `order_id`, `artifact_digest`, `token`) — `Broker.dispatch()` never
returns it (`broker.py:37-39`), so `examples/repl.py` has no way to print it even in verbose mode.

**Failure paths, as presented today** (`examples/repl.py:82-92`):
- `IntentParseError` (malformed/hostile completion, before an `Intent` even exists,
  `intent_parsing.py:40-43`) → `"[parse error -- the model's completion did not satisfy the intent
  schema]"`.
- `GateViolation` and all its subclasses → `"[refused by the Gate/Executor]"`. This one bucket
  covers: an ordinary policy DENY (`execution.py:145-146`), a forged/tampered `Decision`
  (`execution.py:143-144`), an `ArtifactMismatchError` — i.e. a real verification/integrity
  rejection (`execution.py:150-154`, itself a `GateViolation` subclass, `execution.py:53-57`), and
  "no backend registered for this execution_class" (`execution.py:157-158`). All four are raised as
  the same exception family and rendered with the same bracketed label, distinguished from each
  other only by reading the free-text exception message.
- Anything else (including `ExecutionError` — a backend actually running and failing, e.g. a
  nonzero subprocess exit, `execution.py:33-36,116-117`) → `"[unexpected error:
  {type(exc).__name__}: {exc}]"` — visually identical to a genuine programming bug.

## 4. What is already solid

Do not lose this while assessing UX weakness — it is real, verified architecture, not aspiration:

- **The structural non-bypass proof is real and automatically checked**, not merely asserted:
  `tests/test_harness_structural_proof.py` (§2 above). Any future harness front end that reuses
  `CognitiveLoop`/`Broker` as-is inherits this guarantee for free.
- **Core has zero awareness of the harness or of any concrete substrate above the boundary.**
  `siphonophore_core` imports nothing from `siphonophore_harness`; Kubernetes vocabulary is confined
  to `execution_k8s.py` and mechanically enforced absent elsewhere (`tests/test_core_no_k8s_vocabulary.py`).
- **The public composition surface (`Broker.dispatch`, `Executor(gate, backends=...)`,
  `ConsequencePolicy(mapping=..., allowed_kinds=...)`, `Gate.issue_order/grant_root_authority/
  delegate`) is sufficient, unmodified, to reach every substrate and every authority shape that
  exists today** — proven, not assumed: `tests/test_harness_loop_k8s_cluster.py:123-128` builds a
  complete alternate wiring (`ConsequencePolicy(mapping={"k8s": "k8s_pod"})`, `Executor(gate,
  backends={"k8s_pod": ...})`) with zero core changes, and
  `tests/test_harness_loop_k8s_cluster.py:133-150` proves one *shared instance* of that wiring
  answers both a direct `Broker.dispatch()` call and a `CognitiveLoop.step()` call identically.
- **Delegation is real, not a stub.** `Gate.issue_order()` / `grant_root_authority()` / `delegate()`
  independently re-verify their inputs at every step (`mediation.py:70-237`); scope narrowing,
  depth exhaustion, principal mismatch, and forged/spliced authority are all refused
  (`tests/test_authority.py`, full list at line 31-190). Two independently constructed
  `CognitiveLoop` instances sharing one `Gate`/`Broker`, one holding a delegated `Authority`, compose
  correctly through the identical public interface (`tests/test_harness_loop_linux.py:278-321`).
- **Decision integrity is real.** Every dispatch-relevant field (`kind`, `permitted`,
  `execution_class`, `artifact_digest`, `authority_id`, `order_id`) is HMAC-bound
  (`mediation.py:36-44,52-68`); `Executor` independently re-verifies rather than trusting the Gate
  (`execution.py:143-154`); this is exercised adversarially in `tests/test_mediation.py` (kind
  tamper, execution-class downgrade, artifact-digest tamper, permitted-flag tamper — lines 61-104).
- **Belnap-logic self-report/ground-truth reconciliation is a real, independent capability**
  (`audit.py:1-24`), proven end-to-end with a genuinely lying delegated sub-agent
  (`tests/test_harness_loop_linux.py:226-276`).

None of this is presentation-layer work. It is exactly the kind of foundation another harness
builder needs to be able to trust, and none of it needs to change for the UX work this assessment
scopes.

## 5. Operator UX findings

| Finding | Severity | Evidence | Why it matters |
|---|---|---|---|
| No way to see what was authorized (`Decision`) at all, even in `--verbose` mode | BLOCKING | `broker.py:37-39` returns only `Effect`; `examples/repl.py:99-102` has no `Decision` to print | An operator cannot tell what the Gate actually decided versus what the backend claims happened — the two are conflated into one printed object |
| DENY, forged/tampered Decision, integrity (artifact-mismatch) rejection, and "no backend registered" are all presented identically (`"[refused by the Gate/Executor]"`) | BLOCKING | `execution.py:143-158` (four distinct raise sites, one exception family); `examples/repl.py:87-89` (one catch clause, one message template) | A genuine security-relevant distinction (was this refused by policy, or because something was tampered with?) is invisible to the operator without reading source |
| A real backend failure (`ExecutionError`, e.g. nonzero subprocess exit) is shown identically to an unanticipated crash | MINIMUM-BAR | `examples/repl.py:90-92` (bare `except Exception`); `execution.py:33-36,116-117` | An operator cannot distinguish "the artifact I authorized failed on its own" from "something is broken in the harness itself" |
| No `intent_id`/correlation identifier ever printed to the operator, even on success | MINIMUM-BAR | `examples/repl.py:94-102`; `Effect.intent_id` exists (`intent.py:47`) but is never printed by the REPL | Debugging "which turn produced which effect" across a longer session, or correlating with an external observer, requires re-deriving the id from source |
| No default-mode visibility into `execution_class` at all — only under `--verbose` | MINIMUM-BAR | `examples/repl.py:94-102` | The one property Siphonophore exists to make visible (what isolation an action ran under) is opt-in and buried behind a flag, not part of ordinary output |
| Authority/delegation is entirely unreachable from the REPL — no flag, no in-session command | MINIMUM-BAR | `examples/repl.py` has no authority-related code path at all; `--principal-id` is the only identity-adjacent flag (`examples/repl.py:53`) | An operator cannot exercise or observe the single most distinctive capability of this SDK (bounded delegation) without writing Python |
| Only the two portable execution tiers are reachable; `uid_cgroup` and `k8s_pod` cannot be exercised from the REPL under any flag | MINIMUM-BAR | `examples/repl.py:64` (`Executor(gate)`, no `backends=`); confirmed intentional and disclosed in `examples/repl.py:21-23` and README.md:242-247 | An operator cannot see Siphonophore's actual substrate-independence property (the thing the architecture is for) from its own reference surface |
| `Effect.detail` is an ad hoc, backend-specific dict with no common shape across backends | NICE-TO-HAVE (for the REPL itself) / see §10-11 for the foundation angle | compare `execution.py:91` (`detail={}`), `execution.py:118-121` (`acting_pid`, `stdout`), `execution_k8s.py:189-195` (`pod_name`, `namespace`, `node_name`, `phase`, `exit_code`, `stdout`) | Not blocking for a REPL that just prints a dict, but is a real obstacle the moment any structured front end tries to render outcomes uniformly across substrates |
| No installed CLI entry point — must be run as `python3 examples/repl.py` from a repo checkout | NICE-TO-HAVE | `pyproject.toml` defines no `[project.scripts]` entry | Minor friction, not a blocker to "ordinary meaningful use" once the repo is checked out |
| No way to send a message with no action attached — the system prompt requires a `kind` and effectively an `artifact_code` every turn (`prompts.py:49-53`) | OUT-OF-SCOPE | `prompts.py:36-43,49-53` | This is a prompt/protocol shape question upstream of the REPL's own presentation, not something the REPL controls; flagged for awareness only |
| Rich, polished UI (colors, TUI, web front end) | OUT-OF-SCOPE | n/a — not attempted | Explicitly named as a non-goal in the arc's own framing |

## 6. Foundation/composition findings

| Finding | Classification | Evidence | Future-builder consequence |
|---|---|---|---|
| `CognitiveLoop`/`Broker`/`Model`/`intent_parsing` non-bypass property is enforced by static analysis, not convention | SOLID | `tests/test_harness_structural_proof.py:16-56` | Any future harness reusing these classes inherits a machine-checked guarantee for free, not just a documented one |
| Core/harness dependency direction is one-way and mechanically clean | SOLID | `grep` shows zero `siphonophore_harness` imports inside `siphonophore_core/`; `tests/test_core_no_k8s_vocabulary.py` | An external harness can depend on `siphonophore_core` alone with no risk of pulling in the reference harness's own concepts |
| The public composition surface (`Executor(gate, backends=...)`, `ConsequencePolicy(mapping=...)`, `Gate.issue_order/grant_root_authority/delegate`, `Broker.dispatch(intent, authority=...)`) is already sufficient to compose a different substrate and full delegation, with zero core changes | SOLID | `tests/test_harness_loop_k8s_cluster.py:123-128,133-150`; `tests/test_harness_broker.py:57-84`; `tests/test_harness_loop_linux.py:278-321` | A future harness builder does not need a new API to add a substrate or exercise delegation — they need to call constructors the reference harness itself doesn't call |
| `examples/repl.py` is the *only* place that constructs a specific `Executor`/`Policy`/`Gate` wiring for interactive use; no reusable "default reference wiring" helper exists in `siphonophore_harness/` itself | THIN-BUT-VALID | `examples/repl.py:62-65` is inline, ad hoc construction, not a call into a `siphonophore_harness` helper; no such helper exists in `siphonophore_harness/__init__.py` (17 lines, re-exports only) | A second front end (a web harness, a different CLI) wanting "the reference harness's default wiring" must copy `examples/repl.py:62-65` by hand rather than import it — small, but it is duplication, not reuse |
| Granting/orchestrating authority (`issue_order`/`grant_root_authority`/`delegate`, and deciding *when* to construct a second delegated `CognitiveLoop`) has no home anywhere in `siphonophore_harness/` — it exists only inside tests | TEST-FIXTURE QUALITY (for the orchestration side specifically; the underlying `Gate` mechanism itself is SOLID, see above) | `tests/test_harness_loop_linux.py:278-321` constructs both loops and does the delegation calls itself; `siphonophore_harness/` has no equivalent code | This is explicitly acknowledged, not a novel finding: README.md:242-244 and DESIGN.md:551-559 both name "no orchestration layer" as real, open, not-yet-built work |
| `Broker.dispatch()` discards the `Decision` it receives from `Gate.submit()` after using it internally — the caller only ever sees `Effect` (on ALLOW) or a `GateViolation` string (on DENY) | THIN-BUT-VALID — `Decision` is already a fully public, structured core type (`policy.py:17-41`); nothing in core prevents exposing it, this is a Broker-signature choice, not a missing SDK capability | `broker.py:37-39` | A richer front end cannot render "what was authorized" without either re-deriving it or changing what `Broker.dispatch()` returns — see §10 for why this is deliberately left open rather than resolved here |
| `Effect.detail` has no common shape across backends (see §5's table) | COUPLING PROBLEM (mild) — not enforced by any interface, only by convention | `execution.py:91,118-121`; `execution_k8s.py:189-195`; `execution_uid_cgroup.py:266-269` | A front end wanting to render "what happened" uniformly across substrates must special-case every backend's `detail` dict; this is exactly the kind of thing DESIGN.md itself already names as unresolved (see §13's "Explicitly open" citations) |
| Tests construct realistic harness composition, not just fixtures reaching into internals | SOLID | `tests/test_harness_loop_k8s_cluster.py`, `tests/test_harness_loop_linux.py`, `tests/test_harness_broker.py` all drive `Broker.dispatch()`/`CognitiveLoop.step()` through the public interface, never reaching past `Gate`/`Executor` internals | Tests are credible evidence of what a future harness can do, not merely evidence the pieces individually work |
| No pressure observed pushing harness/presentation concerns into `siphonophore_core` | SOLID (no CORE-BOUNDARY RISK found) | `tests/test_core_no_k8s_vocabulary.py` passing; no harness-only concept (Model, Conversation, REPL state) found anywhere under `siphonophore_core/` | The boundary discipline DESIGN.md §10 requires is currently holding; nothing in this assessment argues for weakening it |

## 7. Outcome/error visibility

Inventory of what the operator can currently distinguish, and where that distinction lives:

| Distinction | Structurally represented in core? | Reaches the harness API? | Reaches the REPL operator? |
|---|---|---|---|
| Request accepted vs. malformed completion | Yes (`IntentParseError`, `intent_parsing.py:40-43`) | Yes (propagates from `parse_intent`) | Yes — distinct message (`examples/repl.py:84-86`) |
| Policy ALLOW vs. DENY | Yes (`Decision.permitted`, `policy.py:36`) | `Decision` itself: no (discarded by `Broker.dispatch`, `broker.py:37-39`). DENY as an event: yes, via `GateViolation` (`execution.py:145-146`) | Only as an undifferentiated `GateViolation` message |
| Policy DENY vs. forged/tampered Decision vs. integrity (artifact-mismatch) rejection vs. missing backend | Yes — four distinct raise sites, `ArtifactMismatchError` is a distinct subclass (`execution.py:53-57,143-158`) | Yes, as distinct exception types/subclasses | No — REPL's `except GateViolation` clause collapses all four to one message (`examples/repl.py:87-89`) |
| Verification/integrity failure vs. generic policy denial | Yes (`ArtifactMismatchError` vs. plain `GateViolation`) | Yes | No — same collapse as above |
| Backend execution failure (`ExecutionError`) vs. authorization failure (`GateViolation`) | Yes — separate exception hierarchies (`execution.py:33-36` vs. `mediation.py:240-243`) | Yes | No — `ExecutionError` falls into the REPL's generic `except Exception` branch alongside true bugs (`examples/repl.py:90-92`) |
| Check-in/execution-identity failure (`IdentityError`) vs. authorization failure | Yes — a third, separate exception family (`identity.py:31-34`) | Yes, where a check-in backend is registered | N/A today — the REPL never registers a check-in backend, so this path is unreachable from it, not merely unsurfaced |
| Successful execution vs. execution failure | Yes (`Effect` returned vs. exception raised) | Yes | Yes, but only via the generic catch-all if it's an `ExecutionError` — see above |
| `intent_id`/correlation identity | Yes (`Effect.intent_id`, `intent.py:47`; `Decision.intent_id`) | Yes | No — never printed, even in `--verbose` mode (`examples/repl.py:99-102`) |

The pattern across every row: **core already computes and structurally represents every
distinction that matters.** Nothing here is "absent" or "collapsed into a generic exception" at
the core or harness-API level. What is collapsed is specifically the reference harness's own
presentation layer (`examples/repl.py`'s three `except` clauses). This is the central finding of
this assessment's outcome-model section, and it recurs in §10 and §12: the deficiency is a
presentation gap over already-structured information, not a missing SDK capability.

## 8. Authority/delegation usability

**What an operator sees today:** nothing. `examples/repl.py` has no code path touching `Order`,
`Authority`, `Scope`, `Gate.issue_order()`, `grant_root_authority()`, or `delegate()`. The only
identity-shaped flag is `--principal-id`, which sets a plain string with no authority behind it
(`examples/repl.py:53,65`).

**What must be manually constructed today:** an `Order` (from `Gate.issue_order()`), a root
`Authority` (from `grant_root_authority()`), and any delegated `Authority` (from `delegate()`) —
all three currently only ever called from test code
(`tests/test_authority.py`, `tests/test_harness_broker.py:57-84`, `tests/test_harness_loop_linux.py:278-321`).

**Is the mechanism itself usable through the harness?** Yes, at the `Broker`/`CognitiveLoop` API
level — `Broker.dispatch(intent, authority=...)` and `CognitiveLoop(..., authority=...)` are both
real, tested, public parameters (`broker.py:37`, `loop.py:36`). What is missing is not authority
*exercise* but authority *granting/orchestration*: nothing decides when to delegate, constructs a
second loop, or supplies its model, in a live (non-test) setting. This is not a discovery of this
assessment — it is already explicitly named as open in DESIGN.md's "Explicitly open" section
(DESIGN.md:551-559: "**What remains genuinely open:** that orchestration component itself... doesn't
exist") and in README.md:242-244.

**Is delegation only test-accessible?** Functionally, yes, today — not because the mechanism is
harness-inaccessible (it demonstrably is not, per §6), but because nothing in
`siphonophore_harness/` or `examples/repl.py` calls it outside of tests.

**Would a UX improvement here require changing core semantics?** No evidence found that it would.
`Gate.issue_order/grant_root_authority/delegate` and `Broker.dispatch(authority=...)` are already
public, already sufficient, already exercised successfully by test code standing in for "whatever
orchestrates the agents" (DESIGN.md:556-559's own phrase). What is missing is a harness-level
answer to *who plays that orchestrating role in a live, non-test setting* — an open question,
correctly left unresolved here per this stage's scope (see §16).

## 9. Substrate-selection usability

**Current behavior.** `examples/repl.py` constructs exactly one `Executor(gate)` with the two
default backends and exactly one `ConsequencePolicy()` with the default consequence mapping
(`examples/repl.py:63-64`). Neither is configurable from any flag. This is disclosed as
intentional in the file's own docstring ("Uses only the portable execution tiers... no
root/Linux required. ... runs anywhere", `examples/repl.py:21-23`) and in README.md:242-247 — it
is a known, named limitation of the current reference harness, not an accidental gap this
assessment discovered.

**How backend registration actually works.** `Executor.__init__(gate, backends=None)` accepts an
arbitrary `dict[str, ExecutionBackend]`, defaulting to `{same_process, separate_process}` if
omitted (`execution.py:130-135`); `register_backend()` adds one more after construction
(`execution.py:137-138`). `ConsequencePolicy.__init__(mapping=None, allowed_kinds=None)` accepts an
arbitrary consequence→execution_class mapping (`policy.py:76-82`). Both are ordinary constructor
parameters — there is no registry, plugin system, or discovery mechanism, and none is needed for
what exists today: `tests/test_harness_loop_k8s_cluster.py:123-128` demonstrates a complete
alternate wiring using only these two constructors.

**Does substrate selection belong in reference-harness configuration, in core, or in the caller?**
Composition already happens entirely at the caller/constructor level, which is consistent with
core remaining substrate-neutral (§10's boundary) — no evidence suggests core needs a "backend
registry" concept of its own. Whether the *reference harness* should grow some minimal, discoverable
way for an operator to choose among the substrates that already exist is a real open question, but
CLI syntax, config file shape, or any other mechanism is explicitly out of scope for this stage
(see §16 and the "do not drift into feature design" boundary this arc set).

**Is the current REPL's narrowness intentional or accidental?** Intentional and disclosed
(`examples/repl.py:21-23`), consistent with its stated purpose ("the first genuine end-to-end
validation... does the loop survive contact with actual model output", `examples/repl.py:2-8`) —
it was built to validate the cognitive loop against a real model, not to be a substrate-selection
surface. Whether that original, narrower purpose is still the right scope for the *only* reference
harness this project ships is the requirement-level question this assessment surfaces (§13), not
something this file's authors got wrong for their own stated goal.

**What minimum operator control/discovery is needed?** Left as an open requirement-level question,
not resolved here — see §13, §16.

## 10. Decision/Effect visibility

**Current behavior.** `Gate.submit()` returns a fully-formed `Decision` (`mediation.py:70-122`).
`Broker.dispatch()` receives that `Decision`, hands it to `Executor.execute()`, and returns only
the resulting `Effect` — the `Decision` itself is never returned to the caller
(`broker.py:37-39`). On DENY, `Executor.execute()` raises `GateViolation` before any `Effect`
exists (`execution.py:145-146`) — the `Decision` that recorded the denial (which does exist,
in-memory, and is itself fully verifiable: `tests/test_mediation.py:29` proves "a denied intent
still produces a verifiable decision") is also never surfaced; the caller gets only a string
exception message.

**Is `Decision` currently needed by the operator?** Partially, and only for specific fields.
`execution_class` is already visible (in `--verbose` mode, via `Effect.execution_class`, which
`Executor` copies onto every `Effect` regardless of backend — e.g. `execution.py:91`). What is
*not* visible via `Effect` at all: `permitted` (redundant on ALLOW, since a returned `Effect`
already implies permission — but currently the *only* signal on DENY is an untyped exception
string), `authority_id`/`order_id` (whether and under what delegation this ran), and
`artifact_digest` (what code was actually authorized to run).

**Is loss of Decision only a presentation issue?** For the ALLOW path: yes as far as this
assessment can determine — `Decision` is already a public, structured type
(`policy.py:17-41`); nothing about exposing it violates the substrate boundary or introduces a new
capability, since it carries no more authority than the `Effect` already produced from it. For the
DENY path, it is closer to a genuine API gap: there is currently no `Effect`-shaped object at all
to attach anything to, so surfacing `Decision` there is not purely a print-statement change — it
would require either constructing a `Decision` on the exception, or `Broker.dispatch()` returning
something Effect-and-Decision-shaped instead of raising, both of which are mechanism choices this
stage does not make.

**Would changing `Broker.dispatch()` violate existing abstractions?** No violation of the
mediation invariant was found (returning more information about an already-completed dispatch is
not the same as weakening what gets checked before dispatch completes) — but `Broker`'s own
docstring frames its value specifically as "a caller... can hold exactly one capability --
`dispatch(intent)` -- and nothing else" (`broker.py:1-4`); whether widening its *return* shape is
in tension with that framing, and if so how, is a real design question this stage does not
resolve.

**Could the harness observe Decision through an existing compositional seam?** Only by
constructing `Gate`/`Executor` directly and bypassing `Broker`, which defeats the point of
`Broker` existing at all (per its own docstring, `broker.py:6-10`). No seam was found that lets a
caller observe `Decision` through `Broker.dispatch()` as currently shaped.

**Classification: BOTH — a UX issue (the operator cannot see what was authorized) and a genuinely
open foundation question (whether/how `Broker`'s single-return-value contract should change to
carry it), not resolvable by presentation changes alone on the DENY side.** This matches, rather
than contradicts, README.md's own framing of the same fact (README.md:246-247: "surfaces the
resulting `Effect` but never the `Decision` behind it... so an operator cannot see what was
actually authorized").

## 11. External-builder assessment

Thought experiment, grounded in the code inspected above: a team building a web harness, a
different CLI, an enterprise service, or an agent orchestrator on top of Siphonophore.

| Capability | Reusable directly from core? | Reusable from reference harness? | Trapped in example/test? | Missing? | Architectural concern? |
|---|---|---|---|---|---|
| Mediated dispatch (`Intent` → `Gate` → `Decision` → `Executor` → `Effect`) | Yes | Yes (`Broker`) | No | No | No |
| Adding a new substrate/backend | Yes (`ExecutionBackend`, `Executor(gate, backends=...)`) | N/A — core-level capability | No | No | No |
| Selecting/registering which substrates are active | Yes (constructor args) | No convenience helper exists | No — it's just not built, not hidden | A thin harness-level "default wiring" helper is missing, not a core capability | No — see §6 |
| Issuing/deriving/delegating Authority | Yes (`Gate.issue_order/grant_root_authority/delegate`) | No — reference harness never calls these | Yes — only demonstrated in tests | An orchestration component (who decides *when* to delegate) is genuinely missing | No — this is named open work, not a hidden defect |
| Exercising a held Authority in a cognitive loop | Yes (`Broker.dispatch(authority=...)`, `CognitiveLoop(authority=...)`) | Yes | No | No | No |
| Seeing what was authorized (Decision) alongside what happened (Effect) | Partially — `Decision` type exists and is public | No — `Broker.dispatch()` discards it | No | Yes, for the DENY case specifically | Open design question, see §10 |
| Distinguishing DENY / integrity failure / execution failure programmatically | Yes — distinct exception types exist in core | Not surfaced by the reference harness's own presentation | No — the harness's own catch clauses collapse them | No — the types already exist; a consuming front end can catch them itself without waiting on this reference harness | No |
| Rendering outcome across multiple substrates uniformly | No — `Effect.detail` has no common shape | No | No | A shared outcome shape (if ever needed) does not exist | Mild coupling risk, see §6, deliberately not resolved here |
| Belnap self-report/ground-truth reconciliation | Yes (`audit.py`) | No — reference harness never calls it | Yes — only exercised in tests | Nothing missing at the core level; the reference harness simply never demonstrates it | No |
| A default, minimal interactive front end to point at as a working example | N/A | Yes, with the caveats in §5 | N/A | N/A | This is the substance of the UX findings above |

**Overall:** an external builder can reuse essentially everything at the `siphonophore_core` layer
today without any reference-harness dependency, exactly as DESIGN.md §6 requires
("nothing in `siphonophore_core` imports `siphonophore_harness`", DESIGN.md:210-214, verified in
§2/§6 above). What they cannot get *from the reference harness* is a demonstrated pattern for:
composing more than the two portable backends, orchestrating delegation across multiple loops, or
presenting Decision/Effect/outcome information richly — because the reference harness itself
doesn't do any of those three things yet, not because core prevents them.

## 12. Core vs. harness vs. presentation responsibility

| Layer | What it owns today | Evidence |
|---|---|---|
| CORE / SDK | `Intent`, `Effect`, `Decision`, `Order`, `Authority`, `Scope`, `Gate`, `Executor`, `ExecutionBackend`, `Policy`, `audit.py` reconciliation. All outcome distinctions (DENY vs. forged vs. integrity-failed vs. execution-failed vs. identity-failed) already exist as distinct types here. | §2, §7, §10 |
| REFERENCE HARNESS (`siphonophore_harness/`) | `CognitiveLoop`, `Broker` (composes Gate+Executor behind one call), `Model`/`ScriptedModel`, `parse_intent`, `DEFAULT_SYSTEM_PROMPT`. No orchestration helper, no default-wiring helper, no authority-granting call site. | §6, §8 |
| PRESENTATION / EXAMPLE (`examples/repl.py`) | argparse, the `input()` loop, all `print()` formatting, all three `except` clauses that collapse distinct core exception types into three buckets. | §3, §5, §7 |

For every UX requirement found in §5 and §13: is core missing an SDK capability, or is the
reference harness (harness or presentation layer) simply failing to expose one that already
exists? **In every case examined except the Decision/DENY question in §10, the answer is: the
reference harness is failing to expose an existing core capability.** Core is not missing
anything examined in this assessment. The one genuinely open question — whether `Broker`'s return
contract should widen to carry `Decision` alongside `Effect`, particularly on the DENY path where
no `Effect` object exists to attach it to — sits at the harness-API boundary (`Broker`, not core),
and is named as open rather than resolved (§10, §16).

## 13. Minimum acceptable UX requirements

Each tested against current code and marked REQUIRED / PROBABLY REQUIRED / NOT REQUIRED FOR
MINIMUM BAR / OPEN. Requirement-level only — no mechanism implied.

1. A user can start the harness without studying internals beyond the documented setup (API key,
   model id). **NOT REQUIRED FOR MINIMUM BAR to change** — already true today (`examples/repl.py:10-19`);
   the remaining friction (no installed CLI entry point) is NICE-TO-HAVE, not blocking.
2. Available execution capability/substrate is discoverable by the operator. **PROBABLY REQUIRED**
   — today it is neither discoverable nor selectable (§9); at minimum an operator should be able to
   learn what substrate their session is actually using.
3. A user can submit an ordinary request and see it succeed. **NOT REQUIRED FOR MINIMUM BAR to
   change** — already works today (§3).
4. Authority context is understandable enough to operate safely. **OPEN** — today there is no
   authority context in the REPL at all (§8); whether "minimally acceptable" requires the REPL to
   expose authority/delegation, or only requires that the underlying mechanism remain usable by a
   harness that chooses to, is not resolved by this assessment.
5. ALLOW and DENY are visibly distinct to the operator. **REQUIRED** — today DENY is visible only
   as an unstructured exception string, structurally indistinguishable in presentation from a
   forged-Decision or integrity rejection (§5, §7).
6. Execution success and execution failure are visibly distinct. **REQUIRED** — today a real
   `ExecutionError` is presented identically to an unanticipated crash (§5, §7).
7. Verification/integrity failure is not presented as generic execution failure. **REQUIRED** —
   today it is presented as generic *authorization* failure (folded into the same `GateViolation`
   bucket as DENY), which is a related but distinct problem from being folded into execution
   failure; either way it is not distinguishable to the operator today (§7).
8. Relevant execution/correlation identity (`intent_id` at minimum) is visible enough for
   debugging. **REQUIRED** — never printed today, even in `--verbose` mode (§5).
9. The operator can understand what Siphonophore decided (Decision) and what actually happened
   (Effect), as two separate facts. **PROBABLY REQUIRED, OPEN on mechanism** — see §10; the ALLOW
   side looks straightforwardly addressable, the DENY side is a genuine open question about
   `Broker`'s contract.
10. Routine use does not require reading source code. **PROBABLY REQUIRED** — today, distinguishing
    the outcome categories in requirements 5-7 above requires reading `execution.py` and
    `mediation.py` directly; the system prompt and error strings alone do not convey it.
11. Richer front ends can consume reusable harness logic rather than scraping REPL output. **NOT
    REQUIRED FOR MINIMUM BAR to change today** — already true at the `Broker`/`CognitiveLoop` layer
    (§6, §11); nothing about REPL's own print formatting currently blocks a second front end from
    being built against the same classes, since the REPL owns no logic a second front end would need
    to scrape.

## 14. Foundation requirements

Requirement-level only, not APIs:

1. A future harness builder must be able to construct a working alternate substrate/policy wiring
   using only public constructors, with no core changes. **Already satisfied today** — preserve
   this property, do not regress it (§6, §11 evidence).
2. The non-bypass structural guarantee (`test_harness_structural_proof.py`) must continue to hold
   for whatever the reference harness becomes. **Already satisfied; must remain a required
   invariant of any UX change**, not merely a nice property of the current implementation.
3. Core must remain free of harness-specific or substrate-specific vocabulary. **Already satisfied
   and mechanically enforced** (`test_core_no_k8s_vocabulary.py`); any future requirement that
   appears to need a core change should first be checked against whether the reference harness is
   simply failing to expose something that already exists (§12's rule).
4. If Decision/outcome information is to become visible to operators or richer front ends, the
   representation must be decided once, deliberately, and bound the same way every other
   dispatch-relevant field already is — not accreted ad hoc across backends the way `Effect.detail`
   currently is (§6, §10). This is a requirement that a decision be made carefully, not a
   prescription of what the decision should be.
5. An orchestration concept (who decides when to delegate, constructs a second loop, supplies its
   model) is a real gap for any harness — including a future one — that wants delegation to be
   more than hand-driven test code. This is a requirement that the gap be tracked, not a
   requirement that this stage design the orchestrator (see §16).

## 15. Explicit non-goals

Confirmed not attempted and not implied by anything above:

- A polished product UI.
- A universal agent framework.
- A general orchestration platform.
- Redesigning core merely for REPL convenience.
- Kubernetes-specific concepts entering core (verified absent, §2/§6).
- AgentWatch integration.
- General authority-execution theory drawn from outside this repository.
- Recursive/general provenance beyond what `Order`/`Authority`/`Scope` already model.
- Any import of, or reasoning drawn from, `~/research`.

## 16. Open design questions

Limited to questions that materially affect the next (target-state/delta design) stage:

1. Should `Broker.dispatch()`'s return contract widen to carry `Decision` (or an equivalent) on
   both ALLOW and DENY, and if so, what does that do to `Broker`'s "one capability" framing
   (§10)? This is the single highest-leverage open question — several UX findings (§5, §13.5,
   §13.9) trace back to it.
2. What, if anything, should the reference harness do to make substrate selection discoverable
   without turning into a general plugin/config system (§9)? Requirement-level need is probable
   (§13.2); mechanism is unresolved and explicitly deferred.
3. Should the reference harness grow any orchestration concept for delegation (deciding when to
   delegate, constructing a second loop), or does that belong entirely to whatever *other* harness
   or product eventually needs it (§8, §14.5)? DESIGN.md and README.md both currently treat this as
   open, not merely deferred by this assessment.
4. Should `Effect.detail` ever be given a shared shape across backends, or is per-backend detail
   an acceptable permanent property of the substrate boundary (§6, §11)? Not urgent, but worth
   resolving before more than one or two backends are expected to be rendered by the same front
   end.
5. Is a reusable "default wiring" helper (the composition currently done inline in
   `examples/repl.py:62-65`) worth extracting into `siphonophore_harness/` itself, given it is the
   one piece of duplication a second front end would otherwise have to copy by hand (§6)?

## 17. Evidence / source map

| Finding area | Source |
|---|---|
| Structural non-bypass proof | `tests/test_harness_structural_proof.py` |
| Core/harness import direction | `siphonophore_core/*.py` (no `siphonophore_harness` imports); `siphonophore_harness/{loop,broker,intent_parsing,__init__}.py` (import core) |
| No Kubernetes vocabulary above the substrate boundary | `tests/test_core_no_k8s_vocabulary.py` |
| REPL entry point, flags, wiring, print/except behavior | `examples/repl.py` |
| CognitiveLoop turn logic | `siphonophore_harness/loop.py` |
| Broker single-capability composition | `siphonophore_harness/broker.py` |
| Intent parsing / schema / untrusted-completion handling | `siphonophore_harness/intent_parsing.py` |
| System prompt / protocol taught to the model | `siphonophore_harness/prompts.py` |
| Decision minting, verification, Order/Authority/delegate | `siphonophore_core/mediation.py` |
| Authority/Order/Scope data model and its documented guarantees | `siphonophore_core/authority.py` |
| Intent/Effect data model | `siphonophore_core/intent.py` |
| Policy interface and default mapping | `siphonophore_core/policy.py` |
| Executor dispatch, exception hierarchy, backend registration | `siphonophore_core/execution.py` |
| K8s backend shape, disclosed limitations | `siphonophore_core/execution_k8s.py` |
| Check-in identity, distinct `IdentityError` family | `siphonophore_core/identity.py` |
| Belnap reconciliation | `siphonophore_core/audit.py` |
| Alternate substrate wiring with zero core changes, shared-instance proof | `tests/test_harness_loop_k8s_cluster.py` |
| Delegation through the public Broker/CognitiveLoop interface | `tests/test_harness_broker.py`, `tests/test_harness_loop_linux.py`, `tests/test_authority.py` |
| Decision-tamper adversarial coverage | `tests/test_mediation.py` |
| DENY/forged/artifact-mismatch/no-backend distinct raise sites | `tests/test_execution.py` |
| Project's own acknowledgment of these gaps | `README.md` ("Current state", "Not yet implemented or integrated", "Running the reference harness" sections); `DESIGN.md` §6, §7, "Explicitly open, not yet resolved" |

## 18. Recommended next stage

**Target-state/delta design** for the reference harness's operator UX and foundation, scoped to
the requirements in §13-14 and the open questions in §16 — specifically: how (not whether) to
surface Decision/outcome distinctions, whether/how to make substrate selection discoverable, and
whether an orchestration concept belongs in the reference harness at all. Not implementation.
