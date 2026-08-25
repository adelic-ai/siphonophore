# Experiment 003: Execution class selection

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `f9a7f420158778c2e8f247f00a998b5c4eb87753` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

`DESIGN.md` section 2: execution class follows required authority/consequence, not capability
type. Adding a `consequence` field to `Intent` and having `Gate._policy` map it to an
`execution_class` (`same_process` | `separate_process`): (1) a low-consequence intent runs
same-process (ground truth: the pid that performed the effect matches this process's own pid); (2)
a high-consequence intent runs in a real, separate OS process (ground truth: a different real pid,
not a same-process function pretending to be one); (3) a forged Decision is refused, same as
lab/001-002; (4) a genuinely-minted `separate_process` Decision cannot be relabeled `same_process`
and reused — the strong isolation it was actually granted can't be dodged by downgrading the label
after the fact.

**Null:** if execution class doesn't actually change where the effect runs (both consequences
produce the same pid), or if the class field can be silently downgraded on a real token, the claim
that execution class is a real, enforced decision — not just a string attached to a `Decision`
nothing downstream consults — is falsified.

Claim (4), like lab/002's `kind`-binding finding, isn't testable until there's more than one value
for the field being added. Tested the security-relevant direction specifically: downgrading a
`separate_process` grant to `same_process`, not the reverse — escaping *into* stronger isolation
than granted isn't the attack that matters here.

## Method

- Python 3.14, macOS, no root/Linux required. `separate_process` here means a real
  `subprocess.run([sys.executable, "-c", ...])` — a genuine second OS process — not uid/cgroup
  separation, which is explicitly deferred (see Next steps) and needs colima.
- `Decision` gained an `execution_class` field, bound into the HMAC alongside `kind` (already bound
  since lab/002) — same discipline, applied again rather than re-derived from scratch.
- The subprocess used for `separate_process` reports its own `os.getpid()` from inside itself via
  stdout; the parent's ground-truth check compares that self-reported pid against its own, not
  against an assumption about what `subprocess.run` must have done.

## Procedure

```bash
cd ~/dev/siphonophore
python3 lab/003_execution_class_selection.py
```

## Results

```json
{
  "low_consequence": {
    "selected_execution_class": "same_process",
    "reported_pid": 77964,
    "ran_in_this_process": true,
    "ground_truth_file_content_correct": true
  },
  "high_consequence": {
    "selected_execution_class": "separate_process",
    "reported_pid": 77965,
    "ran_in_a_different_real_process": true,
    "ground_truth_file_content_correct": true
  },
  "forged_bypass": {
    "executor_raised": "GateBypassError: decision '774823b4-349f-4b12-b19b-513a556dd1c2' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  },
  "execution_class_downgrade_replay": {
    "executor_raised": "GateBypassError: decision '9c7ec5b9-2298-411b-bf03-09096fcdfbf9' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  }
}
```

Exit code: `0` (all four hold). Note the two real, distinct pids in the first two results
(`77964` = this process, `77965` = the spawned subprocess) — the actual proof that execution class
changed where the effect ran, not just what it was labeled.

## Analysis

Claims (1)-(3) confirmed cleanly on the first pass, reusing lab/001-002's `Gate`/`Executor` shape
directly. Claim (4) is the one worth having specifically gone looking for, same as lab/002's
`kind`-binding finding: without deliberately testing it, a `separate_process` Decision's `token`
would still verify fine after `execution_class` was changed to `same_process` on the same object,
because `hmac.compare_digest` only fails if the *bound* fields actually differ from what was
signed — the schema had to be built to bind `execution_class` from the start for this to be caught
rather than discovered later. This is the same shape of gap two experiments in a row now — every
new field a `Decision` carries is a new thing that either gets bound into the authorization or
becomes a lever nothing checks. Worth stating as a standing rule going forward rather than
re-discovering per-field: **any field `Executor.execute()` branches on must be bound into the
token, no exceptions, checked at the moment the field is added, not after.**

No other methodology slip this run.

## Conclusion

Hypothesis supported on all four counts. Execution class is a real, policy-driven, enforced
decision in this shape — a low-consequence intent verifiably runs in this process, a
high-consequence one verifiably runs in a different real process, and neither the authorization
itself nor its execution-class label can be forged or downgraded after the fact. Still narrow:
`same_process`/`separate_process` are the only two classes built; `uid+cgroup`/`container`/`VM`
remain unbuilt, and `separate_process` alone (no uid switch) gives real process isolation but
nothing like the attribution the archived v1 primitives already proved for a real uid/cgroup
boundary.

## Next steps

- **TBD, needs colima/root:** add `uid_cgroup` as a real third execution class, reusing the
  archived v1 `identity.py`/`checkin.py` primitives as the actual backend — the natural next
  escalation, and the first lab experiment that can't stay portable. Per the standing rule above,
  whatever new Decision field this needs (a provisioned uid? a cgroup path?) gets bound into the
  token from the start, not added first and bound later.
- **TBD:** a real `Policy`/`Authority` layer deciding `consequence` from something more principled
  than a hardcoded intent field — right now the caller just declares its own consequence level,
  which a real deployment obviously can't trust from the intent alone.
- Worth reconsidering whether the standing rule above ("bind every field `Executor.execute()`
  branches on") should become an actual runtime assertion in `Gate`/`Decision` rather than
  something each new lab experiment has to remember by convention — a real design question for
  when this graduates out of `lab/` into `siphonophore-core`.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is
- [x] Output artifacts under `lab/out/003/`
- [x] Real root/Linux requirement stated explicitly — none needed for this experiment (real
      uid/cgroup separation is explicitly deferred, not silently skipped)
- [x] If a methodology slip was caught: documented in Analysis (the standing per-field-binding rule,
      generalized from a pattern now seen twice)
