# Experiment 001: Gate blocks unmediated effects

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `022dd0eac4e31a6304295e75cbc81f62c3e41b50` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

Given a minimal `Intent → Gate.submit() → Decision → Executor.execute()` pipeline, an effect (a
file write) can be produced through the mediated path (a real `Decision` minted by `Gate.submit()`)
but cannot be produced by presenting `Executor.execute()` with a hand-forged `Decision` that never
went through `Gate.submit()`.

**Null:** if the forged-`Decision` bypass attempt succeeds in writing a file — i.e.
`Executor.execute()` accepts a `Decision` object it did not itself verify came from a real
`Gate.submit()` call — mediation is not structurally enforced, and the central claim in `DESIGN.md`
section 1 (one gate, no other path to an effect) is falsified for even this smallest possible case.

This is deliberately a narrow, structural claim, not a claim about policy correctness, execution
class selection (`DESIGN.md` section 2), or anything about a real cognitive loop — those are later
experiments. This one asks only: is there a code path that reaches an effect without the gate
having minted the authorization for it.

## Method

- Python 3.14 (system `python3` on macOS — portable claim, no root/Linux required; noted per the
  template's own discipline, since a claim that silently needed root and was only run here would be
  assumed, not verified).
- No external dependencies. `Decision.token` is an HMAC-SHA256 over `intent_id:principal_id:permitted`,
  keyed by a 32-byte secret generated fresh per `Gate` instance and never exposed outside it —
  `Executor` only ever receives a bound `verify()` callback, never the secret itself.
- The "bypass" is a `Decision` constructed directly by the experiment code, with `permitted=True`
  and `token="0" * 64` (a 64-hex-char guess, same length as a real digest, so the comparison is a
  real constant-time HMAC check, not a length-mismatch short-circuit) — never produced by
  `Gate.submit()`.

## Procedure

```bash
cd ~/dev/siphonophore
python3 lab/001_gate_blocks_unmediated_effects.py
```

## Results

Actual output from the run recorded in this write-up (also at `lab/out/001/results.json`):

```json
{
  "mediated_attempt": {
    "decision_permitted": true,
    "executor_raised": null,
    "ground_truth_file_exists_with_expected_content": true
  },
  "bypass_attempt": {
    "executor_raised": "GateBypassError: decision '49daaf4d-788c-4ad0-91ce-90fdda41f52f' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  }
}
```

Mediated write: succeeded, and the ground truth (the file on disk, read back independently of the
`Executor.execute()` return value) confirms the expected content — not just "no exception was
raised."

Bypass attempt: raised `GateBypassError` from inside `Executor.execute()`'s own `verify()` check;
ground truth confirms no file was written at all, not merely that an exception surfaced somewhere.

Exit code: `0` (hypothesis supported both ways).

## Analysis

The result is unsurprising given the code — an HMAC keyed by a secret the attacker never has access
to should fail a forged-token check, that's what HMACs are for. The value of this experiment isn't
"did cryptography work," it's confirming the *shape* is right before anything else gets built on top
of it: `Executor` has no `execute(path, content)` overload that skips `Decision` entirely, and
nothing in this experiment's own harness (including the bypass attempt itself) had to reach for a
special testing hook to attempt the bypass — it used the exact same `Executor.execute()` real
application code would use. That's the actual thing worth having checked before assuming it: it
would have been easy to accidentally leave a convenience method that skips the Decision, or to
verify only `decision.permitted` without verifying the token at all. Neither happened here, but
"neither happened" is now a checked fact instead of an assumption.

No methodology slip caught this run — the first version of the script worked as written.

## Conclusion

Hypothesis supported. For this smallest possible case, `Executor.execute()` structurally requires a
`Decision` that verifies against `Gate`'s own secret; a decision object constructed without going
through `Gate.submit()` is refused, and ground-truth inspection (the file itself, not the function's
return value) confirms no effect occurred. This is the minimum viable proof of `DESIGN.md` section
1's central claim — not the whole claim, which also needs delegation reduced to the same primitive
(next experiment) and a real execution-class decision (section 2), neither built yet.

## Next steps

- **002 (TBD):** add a second `Intent` kind — delegation — through the exact same `Gate`/`Decision`/
  `Executor` shape, and confirm it reduces to the same primitive a file-write does. This is the
  actual test of DESIGN.md's unification claim ("tool call and delegation are the same kind of
  thing"), not yet demonstrated — 001 only used one intent kind.
- **003 (TBD):** real execution-class selection (DESIGN.md section 2) — right now `Executor`
  performs every effect in-process regardless of what the intent needs; nothing yet decides
  same-process vs. separate-process vs. uid+cgroup based on authority/consequence.
- **TBD, needs colima/root:** reuse the archived v1 `identity.py`/`checkin.py` primitives as a real
  `Executor` backend for the uid+cgroup execution class, and re-run this same bypass-attempt
  methodology against a process boundary, not just an in-process HMAC check — a materially
  different (and stronger) claim than what 001 tested, per the Trusted Enough to Run discipline of
  not treating a portable/Mac-only check as equivalent to a real-substrate one.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is
- [x] Output artifacts under `lab/out/001/`
- [x] Real root/Linux requirement stated explicitly — none needed for this experiment, noted above
- [x] If a methodology slip was caught: documented in Analysis (none this run)
