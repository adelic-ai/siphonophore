# Experiment 004: uid+cgroup as a real third execution class

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `0b648bfcf07b0a349382d931a65d9e9ffa40e650` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

Extending lab/003's `same_process`/`separate_process` pair with a third execution class,
`uid_cgroup`, backed by the archived v1 `identity.py` primitives (real `useradd`/cgroupfs, not
reimplemented): (1) a critical-consequence intent runs under a real, distinct, provisioned uid, is
a confirmed member of its own cgroup while running, and its identity is cleanly released afterward;
(2) a forged `uid_cgroup` Decision is refused before any provisioning happens; (3) a genuinely-minted
`uid_cgroup` Decision cannot be downgraded to `same_process` and replayed.

**Null:** if the effect runs as the broker's own uid instead of a distinct provisioned one, if the
provisioning happens even once verification/permission checks would have failed, or if the
strongest tier's grant can be downgraded and reused, the claim that `uid_cgroup` is a real
execution-class tier — not same_process wearing a stronger-sounding label — is falsified.

This experiment cannot stay portable, unlike lab/001-003, and does not pretend to: the script
checks for real root on Linux at startup and exits nonzero with an explanation if it isn't running
there, rather than silently reporting a result it didn't check. That guard was itself run and
confirmed on the Mac before the real run (see Method) — the refusal path is part of what's being
verified, not assumed to work.

## Method

- Confirmed on the Mac first: running the script there exits `2` with an explicit message, not a
  silent skip or a false-positive result.
- Real run: colima (real Linux, real root — `sudo python3 lab/004_uid_cgroup_execution_class.py`
  from `/Users/shunhonda/dev/siphonophore` — note the absolute path; `colima ssh -- bash -c 'cd
  ~/...'` resolves `~` to a different guest-user home, not the virtiofs-mounted path, and fails
  confusingly if used).
- `identity.py` reused directly from `archive/v1-mediation-orchestrator/siphonophore/identity.py`,
  loaded via `importlib.util` as a standalone module rather than `from siphonophore import
  identity` — the archived package's own `__init__.py` still imports `orchestrator.py`, which
  imports `strands`, exactly the dependency `DESIGN.md`'s revision dropped. `identity.py` itself
  has no such import; only the package wrapper around it does. (A real methodology slip, caught
  and fixed before the run — see Analysis.)
- No new `Decision` field: `execution_class` has been bound into the token since lab/003;
  `uid_cgroup` is a new *value* on an already-bound field, not a new field. Tested that this is
  actually true rather than assumed (claim 3), the same discipline lab/002-003 established.

## Procedure

```bash
# Confirm the refusal path on a non-root/non-Linux host:
cd ~/dev/siphonophore
python3 lab/004_uid_cgroup_execution_class.py    # exits 2, explains why

# Real run, on colima:
colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/004_uid_cgroup_execution_class.py"
```

## Results

Real output from the colima run:

```json
{
  "critical_consequence": {
    "selected_execution_class": "uid_cgroup",
    "reported_uid": 59001,
    "broker_uid": 0,
    "ran_under_a_different_provisioned_uid": true,
    "pid_was_in_cgroup_while_running": true,
    "ground_truth_file_content_correct": true,
    "identity_released_cgroup_removed": true
  },
  "forged_bypass": {
    "executor_raised": "GateBypassError: decision 'd5b304ea-f62e-476b-83b1-5ff3ec101c62' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  },
  "uid_cgroup_downgrade_replay": {
    "executor_raised": "GateBypassError: decision 'b03160a1-da57-479f-a05a-c1e1df913b23' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  }
}
```

Exit code: `0`. Broker ran as uid `0` (root, required to provision); the effect ran as uid `59001` —
inside the reserved node range `identity.py` defines (`NODE_UID_MIN`-`NODE_UID_MAX` =
59000-59899), genuinely distinct from the broker.

## Analysis

The import methodology slip (archived package's `__init__.py` pulling in `strands` transitively)
is the actual finding worth recording, not a footnote: it's a live demonstration of exactly the
DESIGN.md section 0 decision (drop Strands, don't let it back in even transitively) actually
mattering in practice the first time archived v1 code got reused, not just staying true in theory.
Fixed by loading `identity.py` as a standalone module via `importlib.util`, bypassing the package
`__init__.py` entirely — the fix is narrow and correct: `identity.py` genuinely has no Strands
dependency of its own, only its package wrapper does.

Everything else confirmed on the first real run. The uid+cgroup mechanics themselves (provisioning,
cgroup membership while running, clean teardown) are not new findings — they're the archived v1
primitives' own already-validated behavior (10/10 on colima, per v1's `STATUS.md`), reused here,
not re-proven from scratch. What *is* new to this experiment: confirming those primitives compose
correctly with the Gate/Decision/Executor shape built in lab/001-003, and that the downgrade-replay
protection (bound since lab/003, no new field needed) actually held for the strongest tier without
requiring new work — a real test of whether lab/003's "bind every branching field" rule was
sufficient, not just a repeat of lab/003's own test.

No other methodology slip.

## Conclusion

Hypothesis supported on all three counts. `uid_cgroup` is a real, enforced third execution-class
tier — the effect provably ran under a distinct, provisioned identity, was a confirmed cgroup
member while running, and could not be forged or downgraded from. All three execution classes in
`DESIGN.md` section 2's short list (`same_process`, `separate_process`, `uid+cgroup`) are now real;
`container` and `VM` remain unbuilt, same status as v1 left them.

## Next steps

- **TBD:** a real `Policy`/`Authority` layer deciding `consequence` from something principled —
  still just a caller-declared field across all four experiments so far, flagged repeatedly, not
  yet addressed.
- **TBD:** the check-in/nonce protocol (also reused-available from archived v1 `checkin.py`) hasn't
  been needed yet because this experiment's uid_cgroup effect is a fully-controlled, synchronous
  one-shot subprocess the Executor directly wrote the script for — not a nested agent with its own
  reasoning loop that needs to prove its identity back asynchronously. The natural experiment that
  *would* need it: combine lab/002's delegation with lab/004's uid_cgroup — a delegated sub-agent
  dispatched under its own provisioned uid, checking in before being trusted with real work. Real
  next composition, not yet built.
- **TBD:** `container`/`VM` tiers remain interface stubs in the archived v1 code and untouched here.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is (including the colima absolute-path note)
- [x] Output artifacts under `lab/out/004/`
- [x] Real root/Linux requirement stated explicitly, AND actually run there (not assumed) — the
      Mac's correct refusal was also confirmed, not just the Linux success path
- [x] If a methodology slip was caught: documented in Analysis (the transitive Strands import via
      the archived package's `__init__.py`, found and fixed before the real run)
