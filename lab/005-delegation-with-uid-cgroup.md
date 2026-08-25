# Experiment 005: Delegation dispatched under its own provisioned uid, trusted only after check-in

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `14f65d35445374fec3e27c73fa0bf2b009ccfd8a` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

Combining 002 (delegation reduces to the same Gate/Decision/Executor primitive as any other
intent) with 004 (uid+cgroup as a real execution class): a `delegate` intent with `uid_cgroup`
execution class spawns a real sub-agent process under its own provisioned uid, and the broker only
treats the sub-agent's result as trustworthy after an independent check-in — a nonce (handed to the
child only via an inherited pipe fd, never argv) plus `SO_PEERCRED` (the kernel's own record of
which real uid holds the connecting end of a Unix socket) both verified, not merely a spawn
succeeding under the right uid.

Four falsifiable predicates:

1. **Happy path** — a genuine check-in succeeds; two independent kernel sources
   (`SO_PEERCRED` and `/proc/<pid>/status`) agree with each other and with the child's own
   self-report; the delegated effect is confirmed on disk; the identity is cleanly released.
2. **Impersonation** — a connection presenting a nonce that does not match what the broker
   generated is refused, and the delegation is reported as failed, not silently trusted.
3. **Timeout** — a sub-agent that never checks in at all is refused after a bounded wait, killed,
   and — the specific gap `004`'s write-up named but did not fix — cleanup (user, cgroup) still
   runs, on this failure path exactly as on the success path.
4. **Forged Decision** — a hand-forged `Decision` claiming `delegate`+`uid_cgroup`, never through
   `Gate.submit()`, is refused before any provisioning happens at all: no user, no cgroup, no
   socket, no subprocess.

**Null:** if the sub-agent's result is trusted without a successful check-in, if a wrong-nonce
connection is accepted, if a never-checking-in sub-agent hangs the broker or leaks its identity on
timeout, or if a forged Decision reaches provisioning, the claim that delegation-under-uid_cgroup is
actually independently verified — not just spawned-and-assumed — is falsified.

## Method

- Real root on real Linux (colima) — checked and confirmed via the refusal path first (macOS:
  exits 1 with an explanation), same discipline as `004`.
- Built entirely fresh: no import, copy, or reuse of any prior check-in implementation (v1's
  `checkin.py`, or the earlier `lab/004` built before the archive was deleted). The nonce/
  `SO_PEERCRED` mechanism here is a new implementation, not resurrected code.
- Reused `004`'s exact provisioning style (real `useradd`/cgroup v2, `preexec_fn`-based privilege
  drop, an inherited-pipe-fd synchronization pattern) as a *pattern* to follow, not code to import —
  every line in this script is written for this experiment.
- New uid range (`63000-63999`) and cgroup root (`siphonophore-exp005`), distinct from `004`'s, to
  avoid any collision if both experiments' state existed on the host at once.
- Release logic moved into a single outer `finally` wrapping the entire provision-through-dispatch
  sequence — deliberately fixing the exact gap `004`'s write-up named and left open (cleanup only
  reached on the success path).

## Procedure

```bash
# Confirm the refusal path on a non-root/non-Linux host:
cd ~/dev/siphonophore
python3 lab/005_delegation_with_uid_cgroup.py    # exits 1, explains why

# Real run, on colima:
colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/005_delegation_with_uid_cgroup.py"
```

## Results

Real output from the colima run, all 18 checks:

```
[PASS] happy path: file content matches
[PASS] happy path: check-in verified
[PASS] happy path: provisioned uid differs from broker
[PASS] happy path: SO_PEERCRED matches provisioned uid
[PASS] happy path: SO_PEERCRED matches /proc/status
[PASS] happy path: self-report matches kernel ground truth
[PASS] happy path: cgroup released
[PASS] happy path: user released
[PASS] impersonation: CheckinError raised
[PASS] impersonation: target file absent
[PASS] impersonation: no leftover user
[PASS] timeout: CheckinError raised
[PASS] timeout: target file absent
[PASS] timeout: no leftover user
[PASS] timeout: no leftover cgroup
[PASS] forged: refused
[PASS] forged: target file absent
[PASS] forged: no provisioning side effects
HYPOTHESIS SUPPORTED
```

Broker ran as uid `0`; the delegated effect ran as uid `63000` — inside this experiment's reserved
range, genuinely distinct from the broker, confirmed by two independent kernel sources
(`SO_PEERCRED` on the check-in connection, and a direct root read of `/proc/<pid>/status`) agreeing
with each other and with the child's own self-report.

Full JSON in `lab/out/005/results.json`.

Verified against the actual host after the run, not just the script's own claims: `/etc/passwd` had
zero `sipho5-*` entries, and no `exec-*` leaf directories remained under
`/sys/fs/cgroup/siphonophore-exp005/` — only the empty root directory itself was left (same minor
pattern `004` had: the per-experiment cgroup *root* directory persists even when its per-run
children don't; removed manually, not by the script).

## Analysis

Everything passed on the first real attempt — genuinely unexpected given the added complexity
(three failure paths, a new IPC protocol, a fix to a previously-named gap all combined into one
experiment), and worth being honest that this is somewhat surprising rather than treating a clean
first pass as unremarkable. The most likely reason: this experiment reused `004`'s *pattern*
(provision before spawn, verify ground truth from a kernel source while the process is confirmed
alive, `preexec_fn` for the privilege drop, an inherited pipe fd instead of argv for a secret) very
closely, without reusing its code — the design mistakes that pattern already avoids (the argv
exposure, the socket-permission bug from v1, the unbound-field replay class of bug) didn't need
rediscovering because the pattern itself already routes around them.

The one thing worth flagging as a real, if narrow, design choice rather than an oversight: the
check-in protocol here binds nonce + `SO_PEERCRED` peer uid, but nothing binds the check-in to the
*specific Decision* that authorized this delegation — a second delegation running concurrently, for
a different Decision but provisioned into the same execution_id namespace collision (not possible
today, since `execution_id` is the Decision's own `intent_id`, which is unique per submission, but
worth naming as an assumption the design currently relies on rather than an independently-enforced
fact) could not be told apart from this one by the check-in protocol alone. Not tested here, since
`intent_id` uniqueness is already relied on elsewhere (Gate binding); flagged for a future
experiment rather than assumed away silently.

No methodology slip this run — a genuine first-pass success, not a claim to be taken at face value
without having looked for one.

## Conclusion

Hypothesis supported on all four predicates, 18/18 checks. Delegation combined with a real
uid_cgroup execution class is not just "spawn under the right uid and trust it" — the broker
independently verifies the sub-agent's identity via two kernel-sourced facts before trusting
anything it reports, refuses cleanly on impersonation and on timeout (with full cleanup on both
failure paths, closing `004`'s named gap), and a forged authorization never reaches provisioning at
all.

## Next steps

- The unbound check-in-to-Decision link named in Analysis — worth a future experiment specifically
  targeting whether two concurrent delegations could be confused for each other, rather than relying
  on `intent_id` uniqueness holding elsewhere in the system.
- `DESIGN.md` §3's Belnap-style reconciliation (self-report vs. ground truth) is now backed by a
  real, if narrow, example: this experiment's own `self_report_uid_matches_kernel` check is exactly
  a T/T corroboration case. No experiment yet constructs a genuine T/F or F/T case (a self-report
  that's actually wrong, or ground truth for something never self-reported) — worth a dedicated
  experiment rather than assuming the reconciliation logic generalizes from a case where the two
  channels happened to agree.
- `container`/`VM` execution classes remain unexplored, same status as `004` left them.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is
- [x] Output artifacts under `lab/out/005/`
- [x] Real root/Linux requirement stated explicitly, and actually run there (refusal path confirmed
      on macOS first, then the real run on colima)
- [x] If a methodology slip was caught: none this run — stated explicitly rather than left silent
