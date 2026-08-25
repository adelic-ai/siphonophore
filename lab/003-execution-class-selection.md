# Experiment 003: Execution class selection

**Date:** 2026-08-25
**Status:** complete
**Repo state:** siphonophore @ `e135515`
**Environment:** portable (any host with Python 3.x)
**Data:** synthetic — see Procedure

## Context

DESIGN.md SS2: "Execution class is a per-intent policy decision... A pure calculation can run
in-process. A public network fetch might want a constrained worker." This experiment builds the
smallest real version of that: two execution classes (`same_process`, `separate_process`), a
`consequence` field on `Intent` that a `Gate` policy maps to a class, and — the actual point of the
experiment — proof that the class assignment changes *where the effect physically runs*, not just
a label attached to the Decision object. 002 already validated the "bind every dispatch field into
the HMAC from the start" discipline on `kind`; this experiment applies the identical discipline to
a second, independent field to confirm it's a general pattern and not a one-off fix specific to
`kind`.

## Hypothesis

Four coupled claims:

1. An Intent with `consequence="low"` is assigned `execution_class="same_process"` by `Gate`, and
   the effect's `acting_pid` — the real `os.getpid()` read at the moment the write happens — equals
   the Executor's own process pid.
2. An Intent with `consequence="high"` is assigned `execution_class="separate_process"`, and the
   effect's `acting_pid` — self-reported by a real `subprocess.run`-spawned child process via its
   own `os.getpid()`, printed to stdout and parsed by the parent, never assumed or computed by the
   parent — differs from the Executor's own process pid.
3. A hand-forged Decision (arbitrary token) is refused, and no file is written — same discipline as
   001/002, on the new field.
4. A genuinely-minted `separate_process` Decision, downgrade-replayed by constructing a new
   Decision object with `execution_class` flipped to `same_process` and the token left
   byte-for-byte unchanged, fails `Gate.verify()` and is refused by `Executor.execute()` — no file
   written.

**Null.** Falsified if: the `same_process` effect's acting pid differs from the Executor's own pid
(meaning "same process" was a label, not a fact); the `separate_process` effect's acting pid equals
the Executor's pid (meaning no real process boundary was crossed, or the parent fabricated the
child's reported pid instead of reading it from the real subprocess); any forged Decision is
accepted; or the downgrade-replayed Decision verifies as `True` or is accepted by the Executor.

## Method

- Python: 3.14, stdlib only (`hmac`, `hashlib`, `secrets`, `dataclasses`, `pathlib`, `tempfile`,
  `uuid`, `subprocess`, `sys`, `json`) — self-contained, no import from 001, 002, or anywhere else.
- `Gate.CONSEQUENCE_TO_CLASS = {"low": "same_process", "high": "separate_process"}` — the policy
  mapping, deliberately trivial since the point of this experiment is proving the mechanism, not
  designing a real policy engine.
- `Gate._canonical()` binds `intent_id`, `principal_id`, `kind`, `permitted`, `execution_class` —
  `execution_class` present from the first line of this file's Gate implementation, matching the
  standing rule DESIGN.md SS2 already states and 002 validated once already on a different field.
- `Executor.execute()`'s `same_process` branch writes the file directly in the calling process and
  records `os.getpid()`.
- The `separate_process` branch spawns `subprocess.run([sys.executable, "-c", _CHILD_PROGRAM, path,
  content], capture_output=True, text=True, check=True)`. `_CHILD_PROGRAM` is a ~4-line standalone
  Python snippet (no imports beyond `json`, `os`, `sys`) that writes the file and prints `{"pid":
  os.getpid(), "path": path}` as its own JSON stdout. The parent parses that JSON — the pid comes
  from the child's own runtime, not from `subprocess.run`'s return value or any parent-side
  computation (`subprocess.Popen.pid` would also be real ground truth, but self-report via the
  child's own `os.getpid()` was chosen deliberately to also exercise the "self-report vs ground
  truth" distinction DESIGN.md SS3 names, even though full DESIGN.md SS3 reconciliation is out of
  scope here — noting for a future experiment that these could be cross-checked against each
  other).
- Downgrade test: `genuine_high_decision = gate.submit(downgrade_intent)` — a real, valid
  `separate_process` Decision — then a new `Decision` object is constructed with every field copied
  except `execution_class` (flipped to `"same_process"`) and `token` (kept identical).

## Procedure

```bash
cd /Users/shunhonda/dev/siphonophore
python3 lab/003_execution_class_selection.py
```

## Results

Full raw output: [`out/003/results.json`](out/003/results.json).

Executor (parent) process pid this run: **79144**.

| Predicate | Result |
|---|---|
| `consequence="low"` -> `execution_class="same_process"` | True |
| same_process effect's `acting_pid` | 79144 (== executor pid) |
| same_process file content matches request | True |
| `consequence="high"` -> `execution_class="separate_process"` | True |
| separate_process effect's `acting_pid` | 79145 (real spawned child, self-reported, != executor pid) |
| separate_process file content matches request | True |
| Forged Decision refused | True |
| Forged file absent | True |
| Downgrade-replay: `Gate.verify()` | False |
| Downgrade-replay: Executor refuses | True |
| Downgrade target file absent | True |

Console tail:

```
[PASS] low consequence -> same_process assigned
[PASS] same_process file content matches
[PASS] same_process acting pid == executor pid
[PASS] high consequence -> separate_process assigned
[PASS] separate_process file content matches
[PASS] separate_process acting pid != executor pid
[PASS] forged decision refused
[PASS] forged file absent
[PASS] downgrade: Gate.verify() returns False
[PASS] downgrade: Executor refuses
[PASS] downgrade target file absent
HYPOTHESIS SUPPORTED
```

Script exit code: `0`.

## Analysis

The headline result is 79144 vs. 79145 — two real, distinct OS-assigned process IDs, with the
second one reported by the process that actually owns it, not inferred by the parent. This is the
part that actually tests the claim: it would be trivial to write an Executor that always sets
`acting_pid = os.getpid()` regardless of execution class (a label with no real effect behind it),
or one that "spawns a subprocess" but has the parent write the file itself and just wraps the
result — both would pass a weaker test that only checked `execution_class` string equality on the
Decision. Reading the pid from the child's own stdout, produced by code running inside that
separate process, closes that gap for this experiment's scope.

The downgrade-replay test is structurally identical to 002's kind-mutation test — same mechanism
(HMAC recomputation diverges the instant a bound field changes), applied to a different field. That
it worked identically on the first attempt, with zero debugging, is itself the finding DESIGN.md
SS2 already generalized from 002's experience: this is not a `kind`-specific fix, it's "any field
Executor branches on must be bound," and this experiment is the second independent confirmation of
that generalization (found empirically by hitting the same class of bug twice before generalizing
it, per HISTORY.md — 003 is where the "twice" would have been if the rule hadn't already been
applied from the start).

One methodology note: `check=True` on `subprocess.run` means a child-side exception (e.g. a bad
path) would raise `CalledProcessError` in the parent rather than silently producing no effect —
that's the desired failure mode for this experiment (loud, not silent), but it means the
`separate_process` branch's failure handling is untested here; a bad path or permission error in
the child would surface as an uncaught exception rather than a `GateViolation`. Not a hypothesis
gap (nothing in the four predicates depends on error-path behavior), but worth naming per DESIGN.md
SS4 as an unexamined trust boundary: what should happen when a separate-process effect fails
partway through is not yet designed.

No other methodology slips this round.

## Conclusion

Hypothesis **supported** (4/4 predicates, 11/11 individual checks). Execution class genuinely
determines where an effect runs — confirmed with two real, distinct process IDs, the separate one
self-reported by the actual spawned process rather than assumed by the parent — not merely what a
Decision's field says. `execution_class`, like `kind` in 002, was bound into the HMAC from this
file's first line, and a downgrade-replay attempt against a genuinely-minted `separate_process`
Decision was correctly refused. DESIGN.md SS2's standing rule ("every field Executor branches on
must be bound into the token, no exceptions") now has two independent empirical confirmations
(`kind` in 002, `execution_class` here), which is the generalization the rule itself claims.

## Next steps

- **004**: add `uid_cgroup` as a third execution class, requiring real root on real Linux — extend
  the identical forged-Decision and downgrade-replay tests to this class, plus new ground-truth
  checks (distinct provisioned uid, real cgroup membership) that self-report/child-stdout alone
  can't provide (a compromised or buggy child could lie about its own uid; the uid check for 004
  needs to come from a parent- or kernel-verified source, e.g. `/proc/<pid>/status` read by the
  root parent, not the child's own `os.getuid()` report).
- Named trust boundary above (separate-process failure handling) — not designed or tested here;
  candidate for a future experiment once the SDK has real error/Decision semantics for partial
  failure.
- DESIGN.md SS3's self-report-vs-ground-truth distinction was touched but not fully exercised
  (child's stdout is itself a form of self-report, just cross-checked here only informally against
  "differs from parent pid," not against an independent kernel-level observation) — 004's uid/cgroup
  ground-truth checks are the more complete version of this idea.

## Reproducibility checklist

- [x] Commit SHA recorded (`e135515`)
- [x] Command runnable from this doc as-is
- [x] Output artifacts under `out/003/`
- [x] No one-shot patches or env vars needed
- [x] No methodology slip on the hypothesis itself; one unexamined trust boundary (separate-process
      failure handling) named honestly in Analysis
