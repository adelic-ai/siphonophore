# Reference Harness Turn Boundary Assessment

Investigation stage. Branch `investigate/reference-harness-turn-boundary`, parent
`5524f016a5081147aeb81ae7363845aa0cfc8fba` (`fix/reference-harness-ux-4b`, "harness: polish
reference repl startup"). No source, harness, or test files were modified to produce this
document; every claim below is either a **SOURCE FACT** (a direct read of the file/line named),
a **REPRODUCED FACT** (a characterization run against the real, unmodified code in this repo, or
an existing test in `tests/`), an **INFERENCE** (a conclusion drawn from source facts, flagged as
such), a **RECOMMENDATION**, or an **OPEN QUESTION**.

## 1. Scope

Diagnose, without fixing:

- **Primary**: how a model completion can be accepted as a valid `Intent`, pass Gate/policy,
  select an execution class, and only then fail because the selected backend requires
  `intent.artifact_code`.
- **Secondary**: what happens to artifact stdout/stderr under `same_process` vs.
  `separate_process`, and why a real URL-fetch turn dumped raw GitHub HTML into the operator
  terminal.

Also characterized per the prompt's instructions: the non-JSON-completion path, and the mismatch
between the model's static capability description and the active `portable` `ExecutionProfile`.

No network access, no Anthropic API call, no k8s/agent-vm/`~/research` access was used anywhere in
this stage. All characterization below runs the real, installed `siphonophore_core` /
`siphonophore_harness` code in this repo's own `.venv`, either via existing tests or via a
temporary script kept outside the repository (`/tmp/.../scratchpad/characterize.py`, never
committed).

## 2. Empirical observations (as reported, unverified against live Claude — REPRODUCED FACT below reproduces the code-level mechanism, not the specific wording of a live completion)

- Prompts `"give me your operating context"` and `"what can you tell me about the content at
  https://github.com/adelic-ai/siphonophore"` both produced:
  `[execution failed] same_process backend requires intent.artifact_code`,
  `execution_class=same_process`.
- A separate prompt asking to read a GitHub URL caused raw `<!DOCTYPE html>...` to print directly
  to the terminal, followed afterward by the ordinary structured turn output
  (`[executed] execution_class=same_process intent_id=...`).
- A following prompt `"What did you find?"` produced
  `[input rejected] completion is not valid JSON: Expecting value: line 1 column 1 (char 0)`.
- The startup banner shows `policy: high->separate_process, low->same_process,
  privileged->uid_cgroup` under `profile: portable`, `execution classes: same_process,
  separate_process` — `uid_cgroup` named in the policy line but absent from the registered classes
  line.

## 3. Current model response contract

**SOURCE FACT** — `siphonophore_harness/prompts.py:16-54`, `DEFAULT_SYSTEM_PROMPT`:

- Required by the prompt's own text: a single JSON object, no fence, no prose outside it; a
  `"kind"` naming `"write_file"` or `"run_artifact"`.
- `"artifact_code"` is documented as **optional at the JSON-schema level** but the prompt text
  is explicit that it is required for *any* intent that should have a real effect: *"artifact_code
  is required for ANY intent that should have a real effect, including 'write_file' ... Omit
  artifact_code entirely ... only if you genuinely have nothing to do this turn."*
- The prompt tells the model there is **"currently no way to send only a message with no action
  attached"** and instructs it to *"use a low-consequence, minimal artifact_code (e.g. code that
  does nothing observable)"* when it wants to say something with no meaningful action behind it.
  This is a prompt-level convention only (see §9 below) — nothing in `intent_parsing.py` or
  `siphonophore_core` enforces or even knows about it.
- `"message"` is documented as optional, purely for the human, never executed/evaluated.
- The prompt explicitly forbids the model from including `intent_id`, `token`, or `decision`
  fields.
- `"consequence"` is documented as one of `"low"`, `"high"`, `"privileged"`, described with
  specific execution semantics (see §15).

This is the entire mechanism by which the model is told to include `artifact_code`: **prompt text
only**. There is no schema/tool-use constraint on the Anthropic API call enforcing it (§10, §14).

## 4. `parse_intent` contract

**SOURCE FACT** — `siphonophore_harness/intent_parsing.py:22-23`:

```python
REQUIRED_FIELDS = ("kind",)
ALLOWED_FIELDS = {"kind", "payload", "consequence", "artifact_code", "message"}
```

`artifact_code` is **not** in `REQUIRED_FIELDS`. Only `"kind"` is required. Any completion
containing an allowed subset of fields, with `kind` present, parses successfully regardless of
whether `artifact_code` is present.

Line 89: `artifact_code=data.get("artifact_code")` — absent from the completion, this evaluates to
Python `None`, not an empty string and not a missing attribute (`Intent.artifact_code` always
exists as a field; its value is `None`).

**REPRODUCED FACT** — existing test `tests/test_harness_intent_parsing.py:29-33`
(`test_defaults_applied_when_optional_fields_absent`), run via the full suite in §"Validation"
below, asserts exactly this: `parsed.intent.artifact_code is None` for a completion containing
only `{"kind": "write_file"}`.

**REPRODUCED FACT** — characterization script, section A (full output in §"Validation"):
a completion shaped like the real "give me your operating context" turn —
`{"message": "...", "kind": "run_artifact", "payload": {}, "consequence": "low"}`, no
`artifact_code` — parses successfully:

```
parse_intent() SUCCEEDED. Intent: Intent(kind='run_artifact', principal_id='human-operator',
  intent_id='...', payload={}, consequence='low', artifact_code=None)
parsed.message: Here is my operating context.
```

Answering the prompt's specific questions:

1. Which fields required from Claude? Only `kind` (parser); prompt text additionally asks for
   `payload`, `consequence`, and (conditionally) `artifact_code`, but only `kind`'s absence raises
   `IntentParseError`.
2. Optional fields: `payload` (default `{}`), `consequence` (default `"low"`), `artifact_code`
   (default `None`), `message` (default `None`, lives on `ParsedTurn`, not `Intent`).
3. Is `artifact_code` required by the model *prompt*? Yes, by prompt text, for any real-effect
   turn (§3).
4. Required by `parse_intent()`? No.
5. Required by the `Intent` *type*? No — `siphonophore_core/intent.py:38`,
   `artifact_code: str | None = None`, a genuinely optional dataclass field with its own
   documented rationale (line 28-30: *"An Intent with no artifact_code produces no digest
   binding; Gate.submit() reflects that as an empty digest, not a fabricated one."*). This is a
   **deliberate, documented core design choice**, not an oversight — see §9/§17.
6. Value when absent: `None` (not `""`, not omitted from the dataclass).
7/8. Yes — proved above by both an existing test and the characterization script.
9-13. See §7 (Gate/Decision) and §8 (Executor/backend) below — traced to exact source, not
   inferred from error text.

## 5. Intent representation

**SOURCE FACT** — `siphonophore_core/intent.py:12-38`. `Intent` is a frozen dataclass:
`kind`, `principal_id`, `intent_id`, `payload` (default `{}`), `consequence` (default `"low"`),
`artifact_code` (default `None`). The class's own docstring (lines 28-30) states the design intent
directly: `artifact_code` being optional is deliberate, tied to DESIGN.md §9's artifact-provenance
digest binding, not an accident of the dataclass definition.

## 6. Gate/Decision behavior

**SOURCE FACT** — `siphonophore_core/mediation.py`, `Gate.submit()` (lines 70-122):

- `permitted, execution_class = self._policy.evaluate(intent)` (line 104) — delegates entirely to
  `Policy.evaluate()`.
- `siphonophore_core/policy.py`, `ConsequencePolicy.evaluate()` (lines 84-87):
  ```python
  permitted = intent.kind in self._allowed_kinds
  execution_class = self._mapping.get(intent.consequence, "same_process")
  return permitted, execution_class
  ```
  This function reads only `intent.kind` and `intent.consequence`. **It never reads
  `intent.artifact_code`.** `permitted=True` and `execution_class="same_process"` are fully
  reachable for an `Intent` whose `artifact_code` is `None`, provided `kind` is in
  `DEFAULT_ALLOWED_KINDS = ("write_file", "run_artifact")` and `consequence == "low"` (or
  defaults to it).
- Back in `Gate.submit()`, line 107: `artifact_digest = digest_of(intent.artifact_code) if
  intent.artifact_code is not None else ""` — an absent `artifact_code` produces an **empty
  string** digest, not an error, not a fabricated hash. This is bound into the Decision's HMAC
  token exactly as any other digest would be (line 108-111) — a Decision over a no-code intent is
  a fully valid, fully verifiable Decision.

**REPRODUCED FACT** (characterization §A): `Gate.submit()` for the no-artifact-code intent above
returns `Decision.permitted = True`, `Decision.execution_class = 'same_process'`.

**Conclusion, source-grounded**: neither `Policy.evaluate()` nor `Gate.submit()` cares whether
`artifact_code` exists. Gate/policy fully accepts and authorizes a code-less Intent.

## 7. Executor/backend preconditions

**SOURCE FACT** — `siphonophore_core/execution.py`, `Executor.execute()` (lines 160-179):

1. `decision.intent_id != intent.intent_id or decision.kind != intent.kind` → `GateViolation`
   (correspondence check, not artifact-code related).
2. `self._gate.verify(decision)` → `DecisionVerificationError` if it fails.
3. `not decision.permitted` → `PolicyDeniedError`.
4. **Lines 168-174**: `if intent.artifact_code is not None: ... ArtifactMismatchError` if the
   digest doesn't match. **This block is entirely skipped when `artifact_code is None`** — no
   check runs, no error is raised here for a code-less intent.
5. Backend lookup (`NoBackendRegisteredError` if none registered for the execution class) — not
   artifact-code related.
6. `return backend.run(decision, intent)` — control passes into the backend with
   `intent.artifact_code` still `None`, unexamined by `Executor.execute()` itself.

`Executor.execute()` **does not require `artifact_code`**. It has a conditional check that simply
does not trigger for `None`.

**SOURCE FACT** — `SameProcessBackend.run()`, `execution.py:104-111`:

```python
def run(self, decision: Decision, intent: Intent) -> Effect:
    if not self._allow_root and _running_as_root():
        raise ExecutionError(...)
    if intent.artifact_code is None:
        raise ExecutionError("same_process backend requires intent.artifact_code")
    namespace: dict = {"payload": intent.payload}
    exec(intent.artifact_code, namespace)
    return Effect(intent_id=intent.intent_id, execution_class="same_process", detail={})
```

**This is the first and only component in the entire pipeline that requires `artifact_code`.**
`SeparateProcessBackend.run()` (lines 126-141) has the identical check at line 129-130,
independently, for its own backend.

**REPRODUCED FACT** (characterization §A, and existing test
`tests/test_execution.py:189-193::test_same_process_backend_requires_artifact_code`, which
constructs `Intent(kind="run_artifact", principal_id="alice", intent_id="i-1",
consequence="low")` with no `artifact_code`, submits it through the real `Gate`, and asserts
`Executor.execute()` raises `ExecutionError`):

```
Executor.execute() RAISED: ExecutionError: same_process backend requires intent.artifact_code
classify_outcome -> OutcomeCategory.EXECUTION_FAILED
```

## 8. Exact missing-artifact failure path (Primary Investigation, answered)

```
Claude completion (no artifact_code, "kind":"run_artifact", "consequence":"low")
    ↓
parse_intent()                  intent_parsing.py:60-91    -> ACCEPTS (only "kind" required)
    ↓  Intent.artifact_code = None
Broker.dispatch()               broker.py:53-64
    ↓
Gate.submit()                   mediation.py:70-122        -> ACCEPTS
    ConsequencePolicy.evaluate()  policy.py:84-87           -> never reads artifact_code
    artifact_digest = ""          mediation.py:107          -> no error for missing code
    Decision(permitted=True, execution_class="same_process")
    ↓
Executor.execute()              execution.py:160-179        -> ACCEPTS
    artifact-digest check SKIPPED (artifact_code is None)   execution.py:168
    backend = self._backends["same_process"]
    ↓
SameProcessBackend.run()        execution.py:104-111        -> REJECTS
    "same_process backend requires intent.artifact_code"    execution.py:107-108
```

**The backend itself — not the parser, not the Gate, not `Executor.execute()`'s own
correspondence/verification/permission/digest checks — is the sole and first rejection site.**
This directly confirms the candidate invariant named in the prompt: *parse-valid Intent does not
imply executable Intent for the selected backend*. Both `SameProcessBackend` and
`SeparateProcessBackend` enforce this identically and independently (execution.py:107-108 and
129-130) — it is a per-backend precondition, not a shared/base-class one (`ExecutionBackend` is an
ABC with no shared implementation, execution.py:80-90).

## 9. Message vs. operation semantics today

**SOURCE FACT**:

- `intent_parsing.py:26-37`, `ParsedTurn` docstring: `message` is *"pure display text -- it is
  never passed to Broker.dispatch(), never reaches Gate or Executor, and has no effect on what
  gets authorized or how"* and is explicitly called *"a harness-only concept"* kept out of
  `Intent` on purpose so that `siphonophore_core` stays free of any conversational concept
  (referencing DESIGN.md §6: *"no `Conversation` concept in the core"*).
- `Intent` itself (§5) has no `message` field at all — `message` exists only on `ParsedTurn`, a
  `siphonophore_harness`-level wrapper.
- The system prompt (§3) tells the model there is *"currently no way to send only a message with
  no action attached"* — i.e. the **reference harness's own prompt convention**, not a core
  concept, instructs the model to always attach some `kind`/`artifact_code`, using an inert no-op
  artifact if it has nothing real to do.

Answering the prompt's classification directly: today, `message` is **(C) optional metadata
carried alongside an Intent, at the harness layer only** — never (A) a first-class presentation
concept `siphonophore_core` recognizes, and never (B) a first-class non-action response type. It
is not an "Intent field" either (it is explicitly excluded from `Intent`, living only on
`ParsedTurn`). There is no primitive today, at any layer, meaning "this turn is conversation-only,
no operation was requested" — the *closest* thing is the prompt-level convention of an inert
no-op `artifact_code`, which is a convention Claude must follow correctly on every single turn,
not a structural guarantee.

## 10. Kinds

**SOURCE FACT** — `policy.py:74`, `ConsequencePolicy.DEFAULT_ALLOWED_KINDS = ("write_file",
"run_artifact")`. These are the only two kinds `ConsequencePolicy` permits by default (anything
else yields `permitted=False`, not a parse error — `parse_intent()` accepts any string as `kind`;
`ALLOWED_FIELDS` in intent_parsing.py governs *field names*, not `kind` *values*).

- Every currently-defined kind (`write_file`, `run_artifact`) is understood, by the system prompt
  (§3, §9 above), as something that is supposed to produce a real effect via `artifact_code`.
- No kind exists meaning "message only" / no-op as a first-class, core-recognized primitive.
- The prompt tells the model to *manufacture* a no-op via minimal inert `artifact_code` under an
  existing `kind` (§3, §9) rather than there being an actual no-op primitive.
- Nothing in `siphonophore_core` or `siphonophore_harness` parsing/Gate/Executor code has any
  awareness of "this artifact_code is a deliberate no-op" as a distinct concept from any other
  artifact_code — it is exec'd identically either way (§7's `SameProcessBackend.run()`).

## 11. URL-request path

Given the real prompt and parser constraints, a completion that would satisfy `"read this URL"`
today, under the *documented* contract, looks like this (illustrative, built strictly from
`prompts.py`'s own example and `ALLOWED_FIELDS`/`Intent` shape — no invented fields):

```json
{
  "message": "I'll fetch that page now.",
  "kind": "run_artifact",
  "payload": {"url": "https://github.com/adelic-ai/siphonophore"},
  "consequence": "low",
  "artifact_code": "import urllib.request\nprint(urllib.request.urlopen(payload['url']).read().decode())"
}
```

This is a plausible reconstruction only — no network call was made and no real completion was
captured or inspected to confirm this exact shape; it is offered to explain the *mechanism*, not
as a transcript.

**Why the real completion without `artifact_code` was nevertheless accepted** (§4/§8):
`parse_intent()` only requires `kind`; a completion describing intent to fetch a URL conversationally
(`message` explaining what it's about to do) but omitting `artifact_code` — whether because the
model judged the turn as informational rather than action-bearing, or failed to follow the
prompt's "always include a no-op artifact_code" instruction — parses and is authorized identically
to one that does include it, all the way to `SameProcessBackend.run()`, where it is rejected. The
prompt's own instruction that "there is currently no way to send only a message" is not backed by
any enforcement — a model choosing not to follow it produces exactly the empirically observed
failure.

**Second empirical prompt's raw-HTML dump** is consistent with a completion that *did* include
`artifact_code` performing the fetch and calling `print()` directly on the raw response body — see
§12 for why that output bypassed all harness presentation.

## 12. `SameProcessBackend` stdout/stderr behavior

**SOURCE FACT** — `execution.py:104-111`. `exec(intent.artifact_code, namespace)` is called with
**no stdout/stderr redirection of any kind**. The artifact code executes with Python's ordinary
`sys.stdout`/`sys.stderr`, which — because `SameProcessBackend` runs in-process, inside the same
Python process running `examples/repl.py`'s own `input()`/`print()` loop — are the REPL's own
real terminal streams. `Effect.detail` is hardcoded to `{}` (line 111) regardless of what the
executed code printed, raised, or otherwise did to those streams.

**REPRODUCED FACT** (characterization §B): running artifact code
`print('hello from artifact code')` / `print('X' * 40)` through the real `Executor`/
`SameProcessBackend`:

```
Effect.detail returned by backend: {}
What redirect_stdout actually caught (i.e. went to the real process stdout stream):
'hello from artifact code\nXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX\n'
```

Answering each sub-question directly, all source-grounded:

- Runs under the current process's real stdout/stderr — yes, unconditionally.
- Is stdout redirected/captured? No — nothing in `execution.py`, `broker.py`, `loop.py`, or
  `repl.py` wraps this `exec()` call in any capture mechanism (`contextlib.redirect_stdout`,
  subprocess, thread with a pipe, etc.). Confirmed absent by direct reading of every file in the
  dispatch chain (§8's diagram) plus the reproduction above.
- Are `print()` calls captured? No, per above — they reach the real terminal.
- `Effect.detail`: always `{}` for `SameProcessBackend`, regardless of artifact behavior — a
  structural fact of the `return Effect(..., detail={})` literal at line 111, not
  content-dependent.
- 1 MB of output: unbounded — no size check anywhere in `execution.py`, `mediation.py`, or
  `outcome.py` (`grep` for size/byte/truncat found nothing in any core or harness file). A 1 MB
  `print()` would write 1 MB directly to the real terminal, uncapped.
- ANSI/control sequences: not filtered, stripped, or escaped anywhere in this path — `exec()`'s
  `print()` writes raw bytes/characters to the real terminal exactly as any ordinary Python
  `print()` would.
- No output bound of any kind is imposed by Siphonophore for this backend.

## 13. `SeparateProcessBackend` stdout/stderr behavior (contrast — NOT symmetrical)

**SOURCE FACT** — `execution.py:126-141`:

```python
proc = subprocess.run(
    [sys.executable, "-c", intent.artifact_code, json.dumps(intent.payload)],
    capture_output=True, text=True, check=True,
)
...
return Effect(intent_id=intent.intent_id, execution_class="separate_process",
              detail={"acting_pid": None, "stdout": proc.stdout})
```

`capture_output=True` means the child process's own stdout/stderr are pipes, not inherited from
the parent — the artifact's stdout **never reaches the real terminal directly**; it is captured
into `proc.stdout` and placed into the structured `Effect.detail["stdout"]`.

**REPRODUCED FACT** (characterization §C): running `print('hello from a real subprocess')`
through the real `Executor`/`SeparateProcessBackend`:

```
Effect.detail returned by backend: {'acting_pid': None, 'stdout': 'hello from a real subprocess\n'}
What redirect_stdout caught from OUR process (should be empty -- subprocess has its own stdout):
''
```

Confirms: nothing leaks to the real process's own stdout; output is fully captured and surfaces
only through `Effect.detail`, which `examples/repl.py`'s `render_turn_result()` then prints in a
mediated, labeled form (`detail={...}`), not as a raw dump.

Differences from `same_process`, all source-grounded, not assumed:

- `stdout`: captured into `Effect.detail["stdout"]` (separate_process) vs. not captured at all,
  goes to the real terminal (same_process).
- `stderr`: captured by `subprocess.run(capture_output=True)` but **only surfaced on failure** —
  `except subprocess.CalledProcessError as exc: raise ExecutionError(f"... {exc.stderr}")`
  (line 136-137). On a *successful* run, `proc.stderr` is captured but never read or placed
  anywhere — silently discarded. `same_process` has no separate stderr concept at all (unredirected,
  goes straight to the real terminal exactly like stdout, indistinguishable from it at that point).
- Size/ANSI bound: still none, for either — a large or ANSI-laden `proc.stdout` is placed
  verbatim into `Effect.detail["stdout"]`, which `render_turn_result()` (`examples/repl.py:147-159`)
  then prints as `detail={...}` with no size limit or sanitization either. The difference is
  *where* the content enters the terminal (structured `detail={...}` line vs. raw direct write),
  not whether it is bounded.

## 14. Effect semantics

**SOURCE FACT** — `siphonophore_core/intent.py:42-49`, `Effect` docstring: *"What an Executor
backend reports having done ... still self-report (DESIGN.md section 3) until reconciled against
independently-observed ground truth ... not verified proof that it did."* `detail: dict[str, Any]`
is backend-defined, free-form — nothing in core constrains its shape, size, or content.
`SameProcessBackend` always reports an empty `detail` even though real, unbounded, unmediated
terminal output may have occurred as a side effect of `exec()` — the *Effect* undersells what
actually happened; the terminal already has more information than the structured record does.

## 15. Non-JSON completion path

**SOURCE FACT** — `intent_parsing.py:69-72`:

```python
try:
    data = json.loads(_strip_code_fence(completion))
except json.JSONDecodeError as exc:
    raise IntentParseError(f"completion is not valid JSON: {exc}") from exc
```

`_strip_code_fence()` (lines 46-57) only strips one exact, well-formed triple-backtick fence —
anything else, including ordinary prose, is left untouched and fails `json.loads()`.

**REPRODUCED FACT** (characterization §D): `parse_intent("Sure, here's some info about X.", ...)`
raises `IntentParseError: completion is not valid JSON: Expecting value: line 1 column 1
(char 0)` — the exact error text reported empirically.

- **Retry**: none. `loop.py:57-61`'s own docstring states plainly: *"A malformed or hostile
  completion (intent_parsing.IntentParseError) ... propagates rather than being swallowed here --
  deciding how to recover ... is a caller-level policy question ... not something this minimal
  loop should decide silently."* `CognitiveLoop.step()` has no retry loop; `examples/repl.py:265-269`
  catches the exception at the REPL level only to print it and continue the input loop — no
  re-prompting of the model happens.
- **JSON extraction/repair**: none beyond the single documented fence-stripping normalization
  (§ above) — no regex extraction, no "find the first `{...}`" fallback.
- **Structured output / JSON schema constraint on the API call**: none.
  `model_anthropic.py:65-76`, `AnthropicAPIModel.complete()` calls
  `self._client.messages.create(model=..., max_tokens=..., messages=..., **kwargs)` with `kwargs`
  containing only `system` (if set) — no `tools`, `tool_choice`, or any response-format parameter.
  This is a raw Messages API text completion; JSON compliance is **entirely prompt-enforced**, not
  API-enforced. Confirmed further by `tests/test_harness_model_anthropic.py`, which exercises every
  kwarg the real call passes (`model`, `max_tokens`, `messages`, `system`) and demonstrates no
  others exist.
- **Does a malformed completion reach Gate at all?** No — `CognitiveLoop.step()`
  (`loop.py:64-65`) calls `parse_intent()` *before* `self._broker.dispatch(...)` (line 67); an
  `IntentParseError` raised at line 65 means line 67 (and everything inside it — Gate, Executor)
  is never reached. This is also why `classify_outcome()`'s `INPUT_REJECTED` category
  (`outcome.py:125-126`) is checked ahead of every Gate/Executor-specific category — it is
  reachable only pre-Gate.

## 16. Active-profile / model-description mismatch

**SOURCE FACT** — `siphonophore_harness/composition.py:112-133`, `portable_profile()` registers
exactly two backends: `same_process` (`SameProcessBackend()`) and `separate_process`
(`SeparateProcessBackend()`). `execution_classes = tuple(sorted(backends.keys()))` (line 107)
→ `("same_process", "separate_process")` — no `uid_cgroup` entry, confirmed structurally (the
tuple is built directly from the `backends` dict's own keys, not a separately-maintained list).

`policy_mapping` is `ConsequencePolicy.DEFAULT_MAPPING` (`policy.py:65-69`):
`{"low": "same_process", "high": "separate_process", "privileged": "uid_cgroup"}` — this mapping
**is included** even though no backend is registered for `"uid_cgroup"` in this profile. If a real
completion set `"consequence": "privileged"`, `Gate.submit()` would happily mint a Decision with
`execution_class="uid_cgroup"` (Gate/policy have no awareness of what's registered downstream —
confirmed §6, `ConsequencePolicy.evaluate()` never consults `Executor._backends`), and
`Executor.execute()` would raise `NoBackendRegisteredError` (`execution.py:176-178`) — a different,
but analogous, "authorized-but-unexecutable" gap to the artifact_code one traced in §8.

This exactly matches the empirically observed banner: `policy: ...
privileged->uid_cgroup` shown alongside `execution classes: same_process, separate_process` — the
banner (`repl.py:116-131`, `render_startup_banner()`) is **derived directly and only from
`ExecutionProfile`** (`profile.policy_mapping`, `profile.execution_classes`), so this mismatch is
visible in the profile itself, not a rendering bug.

**Model-visible capability description** (`prompts.py:30-34`) is a **separate, static,
hand-written string**, entirely independent of `ExecutionProfile`:
*"consequence ... privileged ones run under a separate, real OS identity"*. This text:

- Is a module-level constant (`DEFAULT_SYSTEM_PROMPT`), not generated, templated, or parameterized
  by any profile object.
- Is passed to `AnthropicAPIModel` verbatim: `examples/repl.py:236`,
  `model = AnthropicAPIModel(model=args.model, api_key=api_key, system=DEFAULT_SYSTEM_PROMPT)` —
  no substitution of `profile.execution_classes` or `profile.policy_mapping` occurs anywhere
  between `prompts.py` and the API call.
- Describes `"privileged"` executing under "a separate, real OS identity" — true of the
  `uid_cgroup` *concept* in general (see `siphonophore_core/execution_uid_cgroup.py`, not read in
  depth this stage per the "run every backend only enough to understand" scoping, but named
  consistently elsewhere in DESIGN.md/EXECUTION.md), but **not actually deliverable by the active
  `portable` profile**, which has no `uid_cgroup` backend registered at all.

**Conclusion**: the model receives a *general, static* capability description that is not
derived from, and does not match, the specific `ExecutionProfile` actually wired into this REPL
session. The human operator's terminal banner is accurate (profile-derived); the model's system
prompt is not (static, generic, and in this specific case wrong for `privileged`).

## 17. Invariants that currently hold

- **SOURCE FACT**: Every effect-producing path is mediated by `Gate.submit()` before
  `Executor.execute()` — no code path in `broker.py`/`loop.py` reaches a backend without minting a
  Decision first (`broker.py:53-56`).
- **SOURCE FACT**: `Executor.execute()` independently re-verifies the Decision's HMAC and
  artifact digest (when present) before any backend runs — never trusts the Decision merely
  because it was handed one (`execution.py:163-174`).
- **SOURCE FACT**: A completion cannot name its own `intent_id`, Decision, or token —
  `parse_intent()` always mints a fresh `intent_id` (line 86) and `ALLOWED_FIELDS` excludes
  anything Gate-minted.
- **SOURCE FACT**: `message` never influences authorization — it is structurally excluded from
  `Intent` (§9).
- **SOURCE FACT**: `SeparateProcessBackend` output is fully captured and surfaced only through
  `Effect.detail`, never leaked directly to the operator's real terminal (§13).
- **SOURCE FACT**: A malformed (non-JSON) completion never reaches Gate/Executor at all (§15).
- **SOURCE FACT**: `artifact_code`, when present, is cryptographically bound (digest in the
  Decision's HMAC) and re-verified before execution — a swapped artifact is rejected
  (`ArtifactMismatchError`, `execution.py:168-174`), unrelated to but consistent with the
  missing-artifact-code gap this stage investigates.

## 18. Invariants that currently do NOT hold

- **SOURCE FACT**: "A parse-accepted, Gate-permitted Intent is executable by the backend its own
  Decision selected" does **not** hold — `same_process`/`separate_process` each impose their own
  `artifact_code is not None` precondition that neither the parser nor Gate nor
  `Executor.execute()`'s own checks enforce (§8).
- **SOURCE FACT**: "Every accepted `execution_class` in a profile's `policy_mapping` has a
  registered backend" does not hold for the `portable` profile's `privileged`→`uid_cgroup` entry
  (§16) — structurally analogous to the artifact_code gap: policy selection and backend
  availability are two separately-checked things, and Gate only ever performs the first.
- **SOURCE FACT**: "The model's declared capabilities match what the active profile can deliver"
  does not hold — `DEFAULT_SYSTEM_PROMPT` is static and unrelated to `ExecutionProfile` (§16).
- **SOURCE FACT**: `same_process` artifact output is **not** captured, bounded, or mediated in any
  way — it bypasses `Effect`/presentation entirely and reaches the real operator terminal directly
  and in full (§12), unlike `separate_process` (§13) — this asymmetry itself is real and
  unaddressed, not merely an inconsistency between two backends that happen to differ, but two
  backends with genuinely different output-safety properties presented behind the same
  `ExecutionBackend` interface (`execution.py:80-90`) with no interface-level contract about
  output handling at all.
- **INFERENCE**: "A model turn always results in either a real operation or an explicit, harness-
  recognized no-op" does not hold — the closest thing (§9's prompt-level no-op convention) is
  unenforced prompt text with no structural backing; a model choosing not to follow it (as the
  empirical evidence suggests happened) produces exactly the observed failure mode. This is an
  inference about *why* the empirical failures occurred, not a proven fact about the specific
  completions involved (those were not captured/inspected this stage).

## 19. Security-relevant implications of the raw-stdout finding (§12)

Per the prompt's explicit instruction, classified carefully rather than overclaimed:

- **SOURCE FACT, not inference**: `SameProcessBackend.run()` performs no output capture, no size
  bound, no character/ANSI filtering (§12).
- **What boundary is actually crossed — presentation, not mediation.** The Gate/Decision/digest
  mediation boundary (DESIGN.md §1) is fully intact for `same_process`: the code that ran is
  exactly the code the Decision authorized (digest-checked when `artifact_code` is present,
  §17). What is crossed is a **presentation/capture boundary that this codebase never actually
  built for this backend** — there is no evidence anywhere in `execution.py`, `broker.py`,
  `outcome.py`, or `repl.py` that one was ever intended for `same_process` specifically (contrast
  `separate_process`, which does capture, §13); this reads as **an omitted capability**, not a
  regression of an existing one.
- **Correct classification, among the options the prompt lists**: primarily an
  **evidence-loss / output-capture omission** (the `Effect` under-reports what happened — §14) and
  an **operator-terminal side effect** (real, uncontrolled writes to the human's own terminal from
  code the human did not write, sourced from a model's output). Secondarily, a genuine but
  **bounded** ANSI/control-character injection **surface**: since `exec()`'s `print()` output is
  written completely unfiltered to the real terminal (§12, reproduced), artifact code that emitted
  terminal control sequences (cursor movement, screen manipulation, terminal title changes, in
  some terminal emulators OSC-based clipboard/query sequences) would reach the operator's real
  terminal exactly as typed. This is a real, source-confirmed mechanism (unfiltered `print()` to a
  real TTY), not a hypothetical — but this stage did not test which specific control sequences a
  given terminal emulator would act on, nor whether any downstream consequence (e.g. a terminal
  that echoes a query response back as if typed input) is reachable in this REPL's specific setup;
  that remains unverified and is named as an **OPEN QUESTION** below, not claimed as proven
  exploitability.
- **Not** classified as a sandbox escape or a mediation bypass: `same_process` is, by design
  (DESIGN.md §2), the *lowest*-isolation execution class — running arbitrary authorized code
  in-process, with full access to that process's own resources including its stdout, is exactly
  what `same_process` is documented to mean, not a violation of what it promised. The gap is that
  "full access to the process's stdout" was apparently never weighed as a presentation-layer
  concern specifically for a REPL where that stdout is a live, shared, human-facing terminal.

## 20. Architectural options A–D — comparison, not a selection

| | **A — Action-only contract** | **B — First-class message-only turn** | **C — Response envelope w/ optional action** | **D — Every-turn-execution, made strict** |
|---|---|---|---|---|
| Consistency with current code | Close to §7/§8's *de facto* backend behavior (backends already require code) — would move that requirement earlier, to Gate/parser | Requires a new core-or-harness concept absent today (§9); `Intent`/`Gate` have no "no operation" representation | Closest to §9's actual current shape (`ParsedTurn.message` + `Intent`) — mostly formalizing what already exists at the harness layer | Closest to the *prompt's* stated intent (§3/§9's "always include an artifact_code") but not to what parser/Gate/Executor actually enforce today |
| Change surface | `parse_intent()` and/or `ConsequencePolicy`/Gate would need a new required-field or pre-backend check | New `Intent`-adjacent or pre-Gate concept; likely harness-only per §6's core/harness boundary, but touches `parse_intent()`'s contract | Mostly `siphonophore_harness` (`ParsedTurn`, `CognitiveLoop.step()`); `siphonophore_core.Intent` largely unchanged | `parse_intent()` gains a real required-field check for `artifact_code` when `kind` implies effect; core `Intent` field stays optional (§9's stated design) |
| Effect on core (`siphonophore_core`) | Likely requires touching `Policy`/`Gate` if enforcement moves that early — tension with §6's "no Conversation concept in core" | None required if kept harness-only; some if a no-op `kind`/primitive is judged to belong in core | None — `Intent.artifact_code` already optional by design (§5), envelope concept stays harness-side | None — `Intent` schema unchanged; enforcement stays in `parse_intent()`, already harness-side |
| Effect on harness only | Partial — some enforcement could stay in `intent_parsing.py` without touching core | Yes, if scoped as described | Yes, entirely | Yes, entirely (`intent_parsing.py`'s own `REQUIRED_FIELDS`) |
| Compatibility with external harness builders | Core-level changes affect every harness, including ones with a different UX for "no action" turns | No core impact — external harnesses unaffected unless they want the new no-op primitive | No core impact — an external harness ignoring the envelope concept still uses `Intent` exactly as today | No core impact — a stricter `siphonophore_harness.intent_parsing` doesn't constrain a different harness building its own parser directly against `siphonophore_core` |
| Platform-independent SDK boundary (DESIGN.md §6) | Risks moving a harness/model-adapter concern (conversational contract) downward into core — the direction DESIGN.md §6 calls out as one-way and non-negotiable | Same risk if the no-op primitive is pushed into core rather than kept at `ParsedTurn`/harness level | Keeps the distinction clean: envelope/message stays harness-only, `Intent` stays the only core artifact — matches §9's own current design intent explicitly | Keeps the distinction clean the same way — the requirement is enforced in `siphonophore_harness.intent_parsing`, never in `siphonophore_core.intent`/`policy`/`mediation` |
| Attribution implications | None of A–D directly change `principal_id`/Decision attribution — orthogonal to this question in every option | Orthogonal | Orthogonal | Orthogonal |
| Conversational UX | Every real turn, even pure Q&A, must produce *some* action/no-op artifact — closest to today's already-awkward prompt instruction (§3) | Best conversational UX — a genuine "just answer" turn needs no artifact_code and no Gate/Executor round-trip at all | Good UX — message always present, action optional; matches how `ParsedTurn` already separates the two (§9) | Same awkwardness as A/today — every turn still needs *an* artifact_code, even if it's an inert no-op |
| Falsifiable downside / counterexample | A pure "what's your operating context" question has no natural action — forcing one through Gate/Executor for every conversational turn is exactly the friction §16/§8's empirical failures demonstrate | A harness/model that never learns to distinguish "message-only" from "wants an action" could silently under-execute — needs the model to self-classify correctly, same trust problem `consequence` already has (policy.py's own disclosed limitation, §policy.py:59-63) | If "requested operation" is optional on the envelope, a hostile/careless model could omit it even when the user's intent clearly required an action — same self-classification trust problem as B | Doesn't actually fix the observed failure by itself — it only makes the *existing* prompt convention enforced; still requires the model to correctly judge "genuinely nothing to do" every single turn, the same judgment call already failing empirically (§2, §18) |

**INFERENCE, not a recommendation**: Options B and C both track the boundary DESIGN.md §6 already
draws (conversational concepts live in the harness, `Intent` stays a pure would-be-effect record)
more closely than A or D, which either risk pulling a harness/model-adapter concern into
`siphonophore_core` (A) or leave the self-classification trust problem unaddressed while adding
enforcement friction (D). This is not a selection — no implementation should proceed from this
inference alone; see §21.

## 21. Minimal recommended next experiment/fix boundary (NOT implemented here)

**RECOMMENDATION**: The smallest next engineering stage the evidence in this document actually
justifies is narrowly scoped to `siphonophore_harness` (`intent_parsing.py` and/or `prompts.py`),
not `siphonophore_core`:

- Decide, as a design question (not in this stage), whether the harness-level parser
  (`parse_intent()`) should require `artifact_code` whenever `kind` is one of
  `ConsequencePolicy.DEFAULT_ALLOWED_KINDS` (i.e., move today's *backend-level* precondition,
  §7-§8, one step earlier, to the point where the model's turn is first accepted) — this is
  Option D's shape, confined to the harness, and is the smallest change that would turn today's
  late (`SameProcessBackend`/`SeparateProcessBackend`) rejection into an early, harness-level
  `IntentParseError`, without touching `siphonophore_core.intent`/`policy`/`mediation`/`execution`
  at all.
- Separately and independently, decide whether `DEFAULT_SYSTEM_PROMPT` should be generated (or at
  minimum validated) from the active `ExecutionProfile` rather than kept as a hand-written static
  string (§16) — this is purely a `siphonophore_harness/prompts.py` + `composition.py` question,
  again with no core impact.
- The `same_process` stdout-capture gap (§12/§19) is a separate, independent decision (capture and
  surface via `Effect.detail`, matching `separate_process`'s already-existing pattern, §13) that
  does not depend on resolving the artifact_code question first — `SameProcessBackend.run()`'s
  `exec()` call is the single site involved (execution.py:110).

None of the above should be read as "the fix"; each is named as the smallest unit of work the
current evidence supports investigating next, deliberately not bundled into one change, and
deliberately not selecting among Options A–D on this document's authority alone.

## 22. Falsification / counterexamples

- The claim in §8 ("backend is the sole and first rejection site") would be falsified by any
  Gate/Executor-level code path that raises on missing `artifact_code` before reaching
  `backend.run()`. Checked directly: no such path exists in `mediation.py` or the pre-backend
  portion of `execution.py::Executor.execute()` (§6, §7) — confirmed by reading every line of both
  files, not by absence of an error in one run.
- The claim in §12 ("no capture, no bound") would be falsified by any redirection/capture code
  wrapping the `exec()` call at `execution.py:110`. None found by direct reading of `execution.py`
  in full, `broker.py` in full, and `loop.py` in full — the only capture mechanism anywhere in the
  dispatch chain belongs to `SeparateProcessBackend`, a different class entirely.
- The claim in §16 (static, profile-independent model prompt) would be falsified by any code path
  passing `profile.policy_mapping`/`profile.execution_classes` into `DEFAULT_SYSTEM_PROMPT` or
  constructing a per-profile system string. `grep`-level and direct reading of `prompts.py`,
  `composition.py`, and `examples/repl.py` (the only place `DEFAULT_SYSTEM_PROMPT` is consumed,
  line 236) found none.
- **OPEN QUESTION**: this document infers (§18, last bullet) that the real empirical failures
  (§2) occurred because the model omitted `artifact_code` for genuinely conversational turns,
  consistent with the prompt's own acknowledged ambiguity (§3/§9). The actual raw completions
  from those specific turns were not captured or inspected this stage (no `--verbose` transcript
  was available to this investigation) — this is the one place in this document where the
  precise *mechanism* is proven (§8's traced path) but the specific *triggering completion content*
  for the reported empirical turns is not independently confirmed, only plausibly reconstructed
  (§11).

## Open questions

- What did the actual raw completions look like for the two empirically failing prompts (§2)? Not
  captured this stage — `--verbose` output was not available/inspected. Would upgrade §11's
  reconstruction from plausible to confirmed.
- Does any real terminal emulator in this project's actual macOS testing environment act on
  OSC/cursor-control sequences in a way that has a concrete, demonstrable consequence beyond
  visual disruption (§19)? Not tested this stage — deliberately out of scope (would require
  driving a real terminal with adversarial artifact code, an implementation/experiment action, not
  a diagnosis one).
- Is there an existing or planned mechanism (not found this stage) elsewhere in `experiments/` or
  `lab/` addressing output capture for `same_process`-class backends specifically? `experiments/`
  and `lab/` were not inspected this stage beyond directory listing (out of scope per "no general
  AER drift" and the explicit exclusion of `~/research`); a future stage should check `lab/out/00N`
  entries before assuming §12's gap is entirely novel.
- Which of Options A–D (§20), if any, the project intends to pursue — explicitly left
  undecided by this document, which compares rather than recommends a winner among them (per the
  prompt's own instruction).

---

## Validation

Characterization script (`/tmp/.../scratchpad/characterize.py`, never added to the repository) run
against the real, unmodified `siphonophore_core`/`siphonophore_harness` in this working tree,
`.venv` active, from `~/work/siphonophore`:

```
=== A: completion with NO artifact_code (mirrors 'give me your operating context') ===
parse_intent() SUCCEEDED. Intent: Intent(kind='run_artifact', principal_id='human-operator',
  intent_id='...', payload={}, consequence='low', artifact_code=None)
parsed.message: Here is my operating context.
Gate.submit() SUCCEEDED. Decision.permitted = True Decision.execution_class = same_process
Executor.execute() RAISED: ExecutionError: same_process backend requires intent.artifact_code
classify_outcome -> OutcomeCategory.EXECUTION_FAILED

=== B: SameProcessBackend stdout behavior ===
Effect.detail returned by backend: {}
What redirect_stdout actually caught (i.e. went to the real process stdout stream):
'hello from artifact code\nXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX\n'

=== C: SeparateProcessBackend stdout behavior (contrast) ===
Effect.detail returned by backend: {'acting_pid': None, 'stdout': 'hello from a real subprocess\n'}
What redirect_stdout caught from OUR process (should be empty -- subprocess has its own stdout):
''

=== D: non-JSON completion ===
parse_intent() RAISED: IntentParseError: completion is not valid JSON: Expecting value: line 1
  column 1 (char 0)
```

Targeted existing tests re-run (unmodified): `tests/test_execution.py`,
`tests/test_harness_intent_parsing.py`, `tests/test_harness_loop.py`, `tests/test_repl.py` — 75
passed.

Full portable suite: `python3 -m pytest -q` → **224 passed, 43 skipped** — matches the stated
baseline exactly. No test was modified; no test was added.

No network access, no Anthropic API call, no Kubernetes, no `agent-vm`, no `~/research` access
occurred at any point in this stage.

## Adversarial review

1. Did this stage diagnose rather than fix? Yes — no behavior-changing code was written; the one
   new file is this document.
2. Did any core code change? No.
3. Did any harness code change? No.
4. Did any test source change? No — existing tests were invoked, none edited.
5. Are empirical observations distinguished from static inference? Yes — §2 is marked as reported/
   unverified against a live model; §4/§8/§12/§13/§15/§16 are marked SOURCE FACT/REPRODUCED FACT;
   §11 and part of §18/§22 are explicitly marked as reconstruction/inference, not proven completion
   content.
6. Is the missing-artifact failure path traced to exact source locations? Yes — §8, with file:line
   citations at every hop.
7. Is parser acceptance of missing artifact_code proved or disproved? Proved, via both an existing
   test and a fresh characterization run (§4).
8. Is the first component requiring artifact_code identified? Yes — `SameProcessBackend.run()`
   (`execution.py:107-108`) / `SeparateProcessBackend.run()` (`execution.py:129-130`), independently
   each backend's own precondition, not a shared upstream check (§7-§8).
9. Are SameProcess and SeparateProcess stdout semantics separately established? Yes, with
   contrasting reproductions (§12 vs. §13) — explicitly stated as not symmetrical, per instruction.
10. Is raw terminal output classified carefully rather than overclaimed? Yes — §19 explicitly
    rejects "sandbox escape"/"mediation bypass" framing, names the actual boundary crossed
    (presentation/capture, not mediation), and bounds the ANSI-injection claim to "a real,
    source-confirmed mechanism," not proven exploitability, with the remaining uncertainty named as
    an open question.
11. Is non-JSON completion handling characterized without adding repair logic? Yes — §15
    characterizes only; no repair/retry code was written.
12. Is model capability text compared against active ExecutionProfile? Yes — §16, with exact
    mismatch named (`uid_cgroup` in policy_mapping vs. absent from execution_classes; static prompt
    text vs. profile-derived banner).
13. Are reference-harness concerns kept separate from Siphonophore core? Yes — §20's table has a
    dedicated row for this; §21's recommendation is explicitly scoped to `siphonophore_harness`
    files only, citing DESIGN.md §6's core/harness boundary directly.
14. Are options A–D compared rather than prematurely selecting one? Yes — §20 is a comparison
    table; §21's recommendation is scoped to a next *experiment*, not a selection among A–D.
15. Is `~/research` absent? Yes — never accessed, referenced, or read this stage.

No unsupported claim was identified requiring correction before commit.
