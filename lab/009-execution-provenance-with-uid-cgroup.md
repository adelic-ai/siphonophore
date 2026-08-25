# Experiment 009: Execution provenance combined with uid+cgroup

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `2c980bdf56cbeadd6cbaaf82dbc4c8db8ee3eeb8` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

Combining 008 (artifact digest bound into `Decision`, independently re-verified at execution time)
with 004 (uid+cgroup as a real execution class), the same way 005 combined delegation with
uid_cgroup only once both were independently proven: (1) an authorized artifact runs under a real,
distinct provisioned uid, with both identity (kernel-confirmed uid) and content (matching the
authorized digest) verified together; (2) a swapped artifact under `uid_cgroup` is still caught,
and — the genuinely new question neither parent experiment could answer alone — caught **before**
a real `useradd`/cgroup gets provisioned for it, not after; (3) a forged Decision is refused before
any provisioning; (4) a digest-tamper replay is refused by `Gate.verify()`.

**Null:** if a swapped artifact under `uid_cgroup` still results in a real ephemeral user and
cgroup being created before the mismatch is caught — even if the code never actually runs — that
would mean execution provenance and privilege separation compose *inefficiently and riskily*:
real, if short-lived, system state gets created on the host for something that was never going to
be trusted. Worse, if it went uncaught until *after* the process was spawned, the null would be a
real security regression, not just an efficiency question. Predicate 2 exists specifically to
confirm neither happens.

## Method

- Real root on real Linux (colima), same discipline as `004`/`005`/`006`/`007` — refusal path
  confirmed on macOS first.
- Combines two independently-proven mechanisms without importing either's code: 008's digest
  binding and 004's provisioning pattern, both re-implemented fresh in this file (`DESIGN.md` §0;
  `HISTORY.md`'s no-dependencies incident).
- The provenance check runs as the *first* thing inside `Executor.execute()`, before
  `_execute_uid_cgroup` — and therefore before any `useradd`/cgroup call — a deliberate ordering
  choice this experiment verifies, not assumes.
- New uid range (`64000-64999`) and cgroup root (`siphonophore-exp009`), distinct from every prior
  experiment's, avoiding any collision if multiple experiments' state existed on the host at once.

## Procedure

```bash
# Confirm the refusal path on a non-root/non-Linux host:
cd ~/dev/siphonophore
python3 lab/009_execution_provenance_with_uid_cgroup.py    # exits 1, explains why

# Real run, on colima:
colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/009_execution_provenance_with_uid_cgroup.py"
```

## Results

All 15 checks, real output from the colima run:

```
[PASS] happy path: file content matches program A
[PASS] happy path: provisioned uid differs from broker
[PASS] happy path: kernel uid matches provisioned
[PASS] happy path: self-report matches kernel
[PASS] happy path: digest bound in Decision
[PASS] happy path: cgroup released
[PASS] happy path: user released
[PASS] swapped: ArtifactMismatchError raised
[PASS] swapped: target file absent
[PASS] swapped: NO new user provisioned for rejected artifact
[PASS] swapped: NO new cgroup provisioned for rejected artifact
[PASS] forged: refused
[PASS] forged: no new users provisioned
[PASS] digest tamper: Gate.verify() returns False
[PASS] digest tamper: Executor refuses
HYPOTHESIS SUPPORTED
```

Full JSON in `lab/out/009/results.json`. Verified against the actual host after the run, not just
the script's own claims: zero `sipho9-*` entries in `/etc/passwd`, no leftover `exec-*` cgroup
directories (only the empty per-experiment root remained, same minor pattern every uid_cgroup
experiment since `004` has had — removed manually).

## Analysis

Every predicate passed on the first attempt, including the one this experiment exists to answer:
`predicate_b`'s `no_new_users_provisioned`/`no_new_cgroups_provisioned` checks confirm that
rejecting a swapped artifact genuinely costs nothing in real system state — `useradd` was never
called, `provision_cgroup` was never called, because `ArtifactMismatchError` fires before either
function is reached. This wasn't guaranteed by the individual pieces in isolation: `008` never had
a provisioning step to accidentally run ahead of its check, and `004` never had an artifact check to
possibly run after its provisioning. The ordering had to be a deliberate choice in this combined
Executor, and this experiment is the first place that choice was actually exercised against a real
attempt rather than just being the obvious-sounding order to write the code in.

No methodology slip this run.

## Conclusion

Hypothesis supported on all four predicates, 15/15 checks. Execution provenance and uid+cgroup
compose cleanly: the digest check is cheap and fires first, so a rejected artifact never causes a
real user or cgroup to exist on the host even momentarily, and an artifact that does pass both
checks runs with both properties independently confirmed — a distinct kernel-verified identity and
content matching what was actually authorized.

## Next steps

- Combine further with delegation (`005`) and the shared check-in listener (`006`) — a delegated
  sub-agent, dispatched under `uid_cgroup`, whose own artifact is digest-checked before it's even
  provisioned. Not built here; each prior combination (`005`, `006`) was itself its own experiment,
  and this one follows that same discipline rather than combining everything at once.
- The reference-vs-inline-artifact question from `008`'s write-up remains open and is orthogonal to
  this experiment's finding.
- `container`/`VM` execution classes still remain entirely unexplored.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is
- [x] Output artifacts under `lab/out/009/`
- [x] Real root/Linux requirement stated explicitly, and actually run there (refusal path confirmed
      on macOS first, then the real run on colima)
- [x] If a methodology slip was caught: none this run — stated explicitly rather than left silent
