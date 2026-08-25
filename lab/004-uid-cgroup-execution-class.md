# Experiment 004: uid+cgroup as a real third execution class

**Date:** 2026-08-25
**Status:** complete
**Repo state:** siphonophore @ `899d1af` (script fixed once mid-experiment; see Analysis — the
committed version of this experiment is the post-fix version that actually ran successfully)
**Environment:** real root on real Linux (colima) — this experiment is explicitly **not**
portable, and does not claim to be
**Data:** synthetic — see Procedure

## Context

DESIGN.md SS2 names `uid+cgroup` as a real execution-class tier between `separate_process` and
`container`/`VM`. HISTORY.md records that this was already built and validated once, on real
colima infrastructure, but retracted — not because the mechanics failed, but because it was built
by importing `identity.py` from the deleted `archive/v1-mediation-orchestrator/` tree, violating
the project's no-dependencies principle (DESIGN.md SS0: "siphonophore stands alone," not narrowly
"no `strands` package"). This experiment rebuilds uid+cgroup provisioning **fresh**, entirely
inside this file, without importing, copying, adapting, or otherwise consulting that deleted
code — and, per HISTORY.md's own account, actually runs it as root on colima rather than assuming
portability or trusting code review alone. Two real bugs were found in v1 specifically because
validation happened on real infrastructure; this experiment follows the same discipline.

## Hypothesis

Extending 001-003's discipline to a class that needs real OS privilege:

1. A mediated `write_file` effect under `execution_class="uid_cgroup"` succeeds, confirmed by
   reading the actual file back on disk.
2. The effect runs under a **genuinely distinct provisioned uid** — not the broker's own (the
   broker runs as root, uid 0) — confirmed by the **root parent reading `/proc/<pid>/status`**
   (kernel ground truth, the same source any external OS-level observer per DESIGN.md SS5 would
   use), not merely by the child process's own self-reported `os.getuid()`. The two sources are
   expected to agree, but the check that matters is the kernel-observed one.
3. The provisioned process is confirmed as a **real member of its own cgroup while it is still
   running** — `cgroup.procs` for the execution's cgroup is read and shown to contain the child's
   real pid at a point where the process is independently confirmed alive (`Popen.poll() is None`),
   not after it has already exited.
4. The provisioned uid and cgroup are **cleanly released** after execution: the cgroup directory no
   longer exists, and the ephemeral user's `/etc/passwd` entry is gone (`pwd.getpwnam()` raises).
5. A hand-forged Decision claiming `execution_class="uid_cgroup"` is refused, and — beyond just "no
   file written" — **no privileged side effects occur at all**: no new system user is provisioned,
   no new cgroup directory is created. Verification must happen before any provisioning code runs.
6. A genuinely-minted `uid_cgroup` Decision, downgrade-replayed to `execution_class="same_process"`
   with the token left unchanged, fails `Gate.verify()` and is refused.

**Null.** Falsified if: the mediated write's content doesn't match on read-back; the "provisioned"
uid turns out to equal the broker's own uid, or the kernel-observed uid disagrees with what was
provisioned; cgroup membership can only be confirmed after the process has already exited (i.e.
never genuinely observed "while running"); the cgroup directory or user entry survives after
release; the forged Decision causes any provisioning side effect (even if the file write itself is
blocked); or the downgrade-replayed Decision verifies as `True` or is accepted.

## Method

- Target: colima (real root-capable Linux VM). `uname -a`: `Linux colima 6.8.0-117-generic
  #117-Ubuntu SMP PREEMPT_DYNAMIC ... aarch64 GNU/Linux`. `/etc/os-release`: `Ubuntu 24.04.4 LTS`.
  Python 3.12.3 (colima's system Python — different from the 3.14 used for 001-003 on macOS; stdlib
  only, so this doesn't affect the result).
- Confirmed available on the target before writing provisioning code: `useradd`/`userdel`
  (`/usr/sbin/useradd`, `/usr/sbin/userdel`), `/usr/sbin/nologin`, and a real cgroup v2 unified
  hierarchy mounted at `/sys/fs/cgroup` (`cgroup2` filesystem, controllers: `cpuset cpu io memory
  hugetlb pids rdma misc`). A throwaway `mkdir`/`rmdir` under `/sys/fs/cgroup` was tested manually
  first to confirm root can create and remove leaf cgroup directories directly without extra
  controller delegation for plain process-membership tracking.
- Reserved uid range for this experiment's ephemeral users: `62000-62999`. Chosen (not reused from
  memory of any prior design) by inspecting the target's actual `/etc/passwd` (`UID_MIN=1000`,
  `UID_MAX=60000` in `/etc/login.defs`; highest existing entries were `59000` and `65534`/nobody) —
  `62000-62999` was confirmed empty on the real target before use, and avoids systemd's
  `DynamicUser=` range (`61184-65519`) and any standard `SYS_UID` range (`100-999`).
- `provision_ephemeral_user()` shells out to the real `useradd` binary (`--no-create-home --shell
  /usr/sbin/nologin --uid <uid>`), then reads back the created entry via `pwd.getpwnam()` to confirm
  the assigned uid matches what was requested. `release_ephemeral_user()` shells out to `userdel`.
- `provision_cgroup()` creates a real directory under `/sys/fs/cgroup/siphonophore-exp004/exec-<id>`
  (the kernel auto-populates it with `cgroup.procs`, `cgroup.controllers`, etc. on mkdir — that's
  cgroup v2's own behavior, not this script's). `add_pid_to_cgroup()` writes the target pid as text
  into that cgroup's `cgroup.procs`. `release_cgroup()` refuses to `rmdir` if `cgroup.procs` still
  lists a live member (the kernel would refuse anyway, but the check is explicit and produces a
  clearer error than a bare `OSError` would).
- Privilege drop: `subprocess.Popen(..., preexec_fn=_drop_privileges)` where `_drop_privileges()`
  calls `os.setgroups([])`, `os.setgid(gid)`, `os.setuid(uid)` in the forked child before exec —
  standard privilege-drop ordering (gid before uid, since dropping uid first would remove the
  permission needed to still change gid).
- Synchronization between parent and child: an `os.pipe()`, with the read end passed to the child
  via `subprocess.Popen(pass_fds=...)` and its fd number passed as an argv string (not a shared
  secret — this is a liveness gate between trusted parent and its own child, not an authorization
  mechanism, so passing the fd number via argv here is fine; contrast with HISTORY.md's v1 nonce,
  which *was* a secret and was specifically moved off argv for that reason). The child blocks on
  `os.read(sync_fd, 1)` immediately on startup; the parent adds the pid to the cgroup and captures
  all ground-truth observations before writing one byte to release it.
- Ground truth for uid: `read_real_uid_from_proc()` parses the `Uid:` line of
  `/proc/<pid>/status` — the kernel's own record, read by the root parent about a process it does
  not control the self-report of. This is deliberately distinct from the child's own
  `os.getuid()`, which is also collected and compared, but only as a secondary corroborating
  signal (DESIGN.md SS3's self-report-vs-ground-truth distinction, exercised directly this time).

## Procedure

Refusal path (run first, to confirm it actually works before any real run — DESIGN.md's discipline
of checking that the safety-relevant behavior itself works, not just the code that's supposed to
implement it):

```bash
# On macOS (not Linux at all):
cd /Users/shunhonda/dev/siphonophore
python3 lab/004_uid_cgroup_execution_class.py
# -> REFUSED: ... sys.platform='darwin' ... ; exit 1

# On colima (real Linux), without sudo:
colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && python3 lab/004_uid_cgroup_execution_class.py"
# -> REFUSED: ... euid=502 ... ; exit 1
```

Real run, as root, on colima:

```bash
colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/004_uid_cgroup_execution_class.py"
```

(Note the absolute path — `colima ssh -- bash -c 'cd ~/...'` resolves `~` to the wrong home inside
the VM and fails confusingly; this was flagged in the task brief and avoided from the start.)

## Results

Full raw output (written by the real root run on colima, via the virtiofs-shared filesystem — no
manual copy needed): [`out/004/results.json`](out/004/results.json).

**Refusal path — confirmed working before the real run:**

| Check | Platform | Result |
|---|---|---|
| Non-Linux refusal | macOS (darwin) | `REFUSED: ... sys.platform='darwin' ...`, exit 1 |
| Non-root refusal | colima Linux, euid 502 | `REFUSED: ... euid=502 ...`, exit 1 |

**Real run — as root on colima (`Linux colima 6.8.0-117-generic ... aarch64`, Ubuntu 24.04.4):**

| Predicate | Result |
|---|---|
| Broker (script's own) pid / uid | 6777 / 0 (real root, via `sudo`) |
| `execution_class` assigned for `consequence="privileged"` | `uid_cgroup` |
| Mediated file content matches on read-back | True |
| Provisioned username / uid / gid | `sipho-452a3b89` / **62001** / 1002 |
| Provisioned uid differs from broker uid | True (62001 != 0) |
| Kernel-observed uid (`/proc/<pid>/status`, read by root parent) | **62001** |
| Kernel ground truth matches provisioned uid | True |
| Kernel ground truth matches child's own self-reported uid | True (both 62001) |
| Child pid | 6785 |
| Child confirmed alive at cgroup-membership check time | True |
| `cgroup.procs` for `exec-452a3b89-...` while blocked | `[6785]` |
| Child pid confirmed present in `cgroup.procs` while alive | True |
| Child's self-reported `/proc/self/cgroup` | `0::/siphonophore-exp004/exec-452a3b89-8fae-43bf-a32f-34e22fc43eb3` |
| Cgroup directory released after execution | True |
| Ephemeral user's passwd entry released after execution | True |
| Forged `uid_cgroup` Decision refused | True |
| Forged attempt provisioned zero new users | True |
| Forged attempt provisioned zero new cgroups | True |
| Downgrade-replay (`uid_cgroup` -> `same_process`, token unchanged): `Gate.verify()` | False |
| Downgrade-replay refused by Executor | True |
| Downgrade target file absent | True |

Console tail from the real colima run:

```
[PASS] uid_cgroup assigned for privileged consequence
[PASS] mediated file content matches
[PASS] provisioned uid differs from broker uid
[PASS] kernel ground truth matches provisioned uid
[PASS] kernel ground truth matches child self-report
[PASS] child confirmed alive at membership check
[PASS] child pid confirmed in cgroup.procs while blocked
[PASS] cgroup released after execution
[PASS] ephemeral user released after execution
[PASS] forged uid_cgroup decision refused
[PASS] forged file absent
[PASS] forged attempt provisioned no users
[PASS] forged attempt provisioned no cgroups
[PASS] downgrade: Gate.verify() returns False
[PASS] downgrade: Executor refuses
[PASS] downgrade target file absent
HYPOTHESIS SUPPORTED
```

Script exit code: `0`.

## Analysis

**A real methodology slip was found and fixed on the first attempt — documented here rather than
silently patched, per the task's own instruction and HISTORY.md's precedent.** The first run of the
uid_cgroup path (after the refusal-path checks passed) failed with a genuine `PermissionError`
inside the provisioned-uid child process:

```
PermissionError: [Errno 13] Permission denied: '/tmp/sipho-004-ryaihueg/privileged_effect.txt'
```

Cause: `tempfile.mkdtemp()`, called by the root-owned broker process, creates a directory mode
`0700` — writable only by uid 0. Experiments 001-003 never hit this because every effect in those
experiments was performed either by the broker's own process or by a subprocess that inherited the
broker's own uid (no privilege drop occurred). Experiment 004 is the first one where the process
actually performing the effect has a **genuinely different, less-privileged uid** than the process
that created the scratch directory — and the OS correctly enforced that the provisioned identity
had no access to a directory it wasn't granted access to. This is not a bug in the Gate/Executor
mediation logic being tested; it's the test fixture (a shared scratch directory) not accounting for
the fact that a real uid-separated identity has a real, restricted filesystem view. Fixed by
`os.chmod(workdir, 0o777)` immediately after creating it, with a comment explaining why — a fixture
change, not a security loosening of anything the Gate/Executor pipeline is responsible for. Worth
naming as a design point for a production harness, per DESIGN.md SS4: **a real `uid_cgroup`
executor needs an explicit story for what filesystem access a provisioned identity gets** — this
experiment sidesteps the question by widening one scratch directory, which is fine for a lab
experiment proving the mediation mechanism, but is exactly the kind of narrow, low-consequence
trust boundary DESIGN.md SS4 says should be named rather than silently assumed solved.

**A second, related gap surfaced from that same failed first attempt, found while cleaning up
afterward rather than during the run itself.** `_execute_uid_cgroup()`'s provisioning
(`provision_ephemeral_user`, `provision_cgroup`) happens before the `try/finally` block that only
guards the pipe file descriptors — the release calls (`release_cgroup`, `release_ephemeral_user`)
sit after that block, reached only on the non-exceptional path. When the child's `PermissionError`
propagated up as a `ProvisioningError` on the first attempt, the exception skipped past both
release calls, leaking a real ephemeral user (`sipho-4017a344`, uid 62000) and a real cgroup
directory (`exec-4017a344-...`) on the colima host. Confirmed by checking `/etc/passwd` and
`/sys/fs/cgroup/siphonophore-exp004/` directly after the run — both were still present, found
independently of the PermissionError fix itself. Cleaned up manually (`userdel`, `rmdir`) rather
than left for the next run to trip over. **Not fixed in the script** — this is a second, real trust
boundary named per DESIGN.md SS4 rather than patched under time pressure: a production
`uid_cgroup` executor needs release logic that runs on every exit path (a `try/finally` wrapping
the full provision-through-release sequence, not just the fd handling), so a mid-execution failure
can't leave a live system user or cgroup allocated indefinitely. Left as a named, understood gap
rather than silently closed, consistent with this lab's discipline of writing up what didn't hold
on the first attempt rather than only reporting the eventual clean pass.

Beyond those two fixture/robustness gaps, everything else passed on the first real attempt. The uid separation
is real and observable two independent ways: the kernel's own `/proc/<pid>/status` record (read by
root, about a process root does not control the self-report of) and the child's own `os.getuid()`
agree exactly (62001 both times) — which is expected (there's no reason for them to diverge absent
an attack on the child's own reporting), but confirming they *actually* agree, rather than assuming
it, is the point: DESIGN.md SS3 exists because self-report and ground truth are different claims,
and this experiment is the first one in this lab series to actually collect both independently and
compare them, rather than relying on either alone.

The cgroup-membership-while-alive check is the part most vulnerable to a race if built carelessly —
if membership were checked only after `proc.communicate()` returned, the process could already have
exited and been auto-removed from `cgroup.procs`, silently passing a check that proves nothing. The
pipe-based synchronization (`os.read(sync_fd, 1)` blocking the child until the parent explicitly
releases it) exists specifically to close that gap, and `still_alive_at_membership_check: true`
alongside `child_pid_in_cgroup_while_blocked: true` in the results confirms the check happened when
it was supposed to, not after the fact.

The provisioned uid (62001) and username (`sipho-452a3b89`, derived from the execution's
`intent_id`) are genuinely distinct from any prior run — re-running the experiment would allocate a
different uid in the range and a different username, since both are computed at execution time
rather than fixed constants, matching the "ephemeral" requirement.

## Conclusion

Hypothesis **supported** (6/6 predicates, 16/16 individual checks), actually run as root on real
Linux (colima, Ubuntu 24.04.4 aarch64) — not assumed portable, and the refusal path for both
non-Linux and non-root was confirmed working before the real run was attempted. uid+cgroup is
buildable and testable as a third execution class using the same forged-Decision and
downgrade-replay methodology as 001-003, built entirely fresh with no dependency on the deleted v1
archive. One real, honestly-documented fixture bug (scratch-directory permissions not accounting
for a genuinely uid-separated child) was found and fixed on the first attempt, matching HISTORY.md's
own account of how these gaps get found: by running for real, not by code review.

## Next steps

- The filesystem-access trust boundary named in Analysis (what a provisioned uid_cgroup identity
  can actually read/write) is not designed here — a production Gate/Executor needs an explicit
  per-execution writable directory (or bind-mount) policy, not a lab-experiment `chmod 777`. TBD,
  not yet a numbered experiment.
- The exception-safety gap named in Analysis (provisioning happens outside the `try/finally` that
  guards release) is not fixed here either — a production `uid_cgroup` executor needs release
  logic that runs on every exit path, not just the success path. TBD; the leaked user/cgroup from
  this experiment's own first attempt were cleaned up manually, not automatically.
- `container` and `VM` execution classes (DESIGN.md SS2's remaining tiers) are unexplored — natural
  follow-ups once the SDK's real policy engine exists, likely requiring a real container/VM runtime
  on colima the same way this experiment required real root.
- This experiment did not test resource *limits* via the cgroup (only membership/accounting) —
  `pids.max`, `memory.max`, etc. are available controllers on this target (confirmed present in
  `/sys/fs/cgroup/cgroup.controllers`) but unused here; a future experiment could test that a
  provisioned identity's cgroup limits are actually enforced, not just that membership is tracked.

## Reproducibility checklist

- [x] Commit SHA recorded (`899d1af`, pre-fix; this experiment's own commit records the working,
      post-fix state)
- [x] Command runnable from this doc as-is (refusal-path commands and the real colima command)
- [x] Output artifacts under `out/004/` (written directly by the real colima run via the
      virtiofs-shared filesystem)
- [x] No one-shot patches or env vars beyond the `os.chmod(workdir, 0o777)` fixture fix, which is
      in the committed script itself (not a one-shot — it runs every time)
- [x] Methodology slip documented in Analysis (scratch-directory permission failure, real
      `PermissionError`, found on the first real attempt, fixed with rationale given)
- [x] Confirmed actually run as root on real Linux (colima) — refusal path checked first (both
      non-Linux and non-root cases), then the real run's console output and `out/004/results.json`
      both captured from that real execution
