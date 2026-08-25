# Experiment 007: A genuine Belnap contradiction (T/F) and unreported-activity (F/T) case

**Date:** 2026-08-25
**Status:** complete
**Repo state:** siphonophore @ `797173beef4e197aa7a99491c2d4cb2d6e1cd091` (this script and write-up
land in the commit immediately after this SHA)
**Environment:** real root on real Linux (colima)
**Data:** synthetic — see Procedure

## Context

DESIGN.md §3 describes four-valued reconciliation between self-report and ground truth:

```
claimed  observed
  T         T      → corroborated
  T         F      → contradiction / unsupported claim
  F         T      → unreported activity
  F         F      → no evidence
```

Every experiment in this lab so far (001-006) has only ever produced the T/T corroborated case —
005's own Next Steps named this directly: "No experiment yet constructs a genuine T/F or F/T case
… worth a dedicated experiment rather than assuming the reconciliation logic generalizes from a
case where the two channels happened to agree." Per DESIGN.md's own account, the reconciliation
logic that implements §3's comparison doesn't exist anywhere in the repo yet either. This experiment
does both: implements `reconcile()` as real code, and constructs a real scenario where self-report
and ground truth genuinely disagree.

## Hypothesis

A real reconciliation function, correctly implementing DESIGN.md §3's (claimed, observed) →
Belnap-value comparison, correctly classifies all four cases — including two never demonstrated in
this lab before — using a **real** delegated sub-agent running under its own provisioned uid (the
same uid+cgroup shape as 004/005/006), not synthetic/hand-constructed claim-observation pairs alone:

1. A genuine **contradiction** (T/F): the self-report claims a specific file has specific content;
   ground truth, read independently by the root parent from outside the sub-agent's own uid, shows
   that file actually has *different* content.
2. A genuine **unreported activity** (F/T): the sub-agent performs a real, extra write that ground
   truth confirms happened, but the self-report never mentions it at all.
3. Re-derived alongside the two new cases, not assumed to still hold: a genuine **corroborated**
   case (T/T) and a genuine **no evidence** case (F/F), so the classifier is shown to distinguish
   all four, not just the two being newly introduced.

**Null:** falsified if `reconcile()` misclassifies any of the four basic truth-table combinations;
if the real T/F case's contradiction is not detected (e.g. because ground truth was read in a way
that's derived from or trusts the self-report rather than being independently collected); if the
real F/T case's unreported write is missed (e.g. because ground truth only checks claimed paths
rather than genuinely scanning for what's actually present); or if a forged Decision reaches
provisioning.

## Method

- Real root on real Linux (colima) — refusal path confirmed on macOS (non-Linux) and colima without
  sudo (non-root) before the real run, same discipline as 004-006.
- `reconcile(claimed: bool, observed: bool) -> BelnapValue` is a pure function with no access to how
  either boolean was derived — it cannot be biased toward trusting one channel over the other. A
  second function, `reconcile_path()`, applies it to a specific proposition about a path (either "is
  this exact content claimed/observed at this path" or, when no content is given, "is this path
  claimed/observed at all" — used for the unreported-activity/no-evidence checks, where the whole
  point is that *no* claim mentions the path).
- One real delegated sub-agent execution, dispatched through the same Gate/Decision/Executor
  mediation pipeline as 002/004/005/006 (`delegate` kind, `uid_cgroup` execution class for
  `privileged` consequence), provisions a real ephemeral uid (range `65100-65199`, distinct from
  004/005/006) and cgroup (`siphonophore-exp007`), then spawns a real subprocess under that uid
  that deliberately produces all four situations at once:
  - `corroborated.txt` — written with content X; self-report claims content X. (T/T)
  - `contradicted.txt` — written with content Y; self-report claims *different* content Z — a real,
    active lie, not a missing file. (T/F)
  - `unreported.txt` — written for real; never mentioned in the self-report's claims list at all.
    (F/T)
  - `never.txt` — neither written nor claimed. (F/F, checked by the reconciler but never referenced
    by the sub-agent program at all.)
- Self-report: read from a file the **parent** opened and the **child** inherited as an already-open
  stdout fd (`stdout=stdout_fd`, not `stdout=PIPE`+`communicate()` — 006 found that combination
  raises a real `OSError: Bad file descriptor` under concurrent load on this target; not needed for
  correctness in this single-dispatch experiment, but reused since there's no reason to reintroduce
  a pattern already known to be risky). `user=`/`group=`/`extra_groups=` used for the privilege
  drop, not `preexec_fn`, same reasoning.
- Ground truth: collected by the **root parent**, independently, via a plain `outdir.iterdir()` +
  `read_text()` over the sub-agent's output directory — after independently confirming
  (`proc.wait()`, not merely trusting the self-report) that the child has exited. This code path
  never reads, parses, or references the self-report in any way; it is exactly what an external
  observer per DESIGN.md §5 would see with zero siphonophore-specific code.

## Procedure

```bash
# Confirm the refusal path on a non-root/non-Linux host:
cd /Users/shunhonda/dev/siphonophore
python3 lab/007_belnap_reconciliation.py    # exits 1, explains why

# Real run, on colima:
colima ssh -- bash -c "cd /Users/shunhonda/dev/siphonophore && sudo python3 lab/007_belnap_reconciliation.py"
```

## Results

Real output from the colima run, all 15 checks:

```
[PASS] truth table: T,T -> corroborated
[PASS] truth table: T,F -> contradiction
[PASS] truth table: F,T -> unreported_activity
[PASS] truth table: F,F -> no_evidence
[PASS] real delegation: provisioned uid differs from broker
[PASS] real delegation: kernel-observed uid matches provisioned
[PASS] real delegation: child confirmed exited before ground truth read
[PASS] real T/T case (corroborated.txt) -> corroborated
[PASS] real T/F case (contradicted.txt) -> contradiction
[PASS] real F/T case (unreported.txt) -> unreported_activity
[PASS] real F/F case (never.txt) -> no_evidence
[PASS] real delegation: cgroup released
[PASS] real delegation: user released
[PASS] forged: refused
[PASS] forged: no new users provisioned
HYPOTHESIS SUPPORTED
```

Provisioned uid `65100` (broker ran as root, uid `0`), confirmed distinct and confirmed by the
kernel (`/proc/<pid>/status`, read by the root parent) to match what was provisioned.

Self-report vs. ground truth, side by side (the actual data the reconciler ran against):

| path | self-reported content | ground-truth content | claimed | observed | value |
|---|---|---|---|---|---|
| `corroborated.txt` | "this write really happened and matches the claim" | *(same)* | true | true | **corroborated** |
| `contradicted.txt` | "a claimed content that is simply not true" | "the real content, which differs from what the self-report is about to claim" | true | false | **contradiction** |
| `unreported.txt` | *(not claimed)* | "a real effect that happened but was never self-reported" | false | true | **unreported_activity** |
| `never.txt` | *(not claimed)* | *(does not exist)* | false | false | **no_evidence** |

Full JSON in `lab/out/007/results.json`.

Host verified clean after the run, directly against `/etc/passwd` and `/sys/fs/cgroup` (not the
script's own claims): zero `sipho7-*` entries, zero `exec-*` leaves under
`/sys/fs/cgroup/siphonophore-exp007/`, zero live processes in the `65100-65199` uid range, zero
leftover `/tmp/sipho-007-*` files.

## Analysis

All 15 checks passed on the first real attempt — no methodology slip this run, stated honestly
rather than assumed unremarkable (matching 005's own precedent for saying so plainly when it's
true, not just when there's a gap to report).

The result confirms the specific thing the null was designed to catch: `contradicted.txt` genuinely
**exists** with genuine, real content — this is not a missing-file case, which would be a much
weaker test (any reconciler that only checks "does the claimed path exist at all" would trivially
pass a missing-file contradiction). The self-report's claim about *what content* is at that path is
specifically false, and `reconcile_path()`'s content-aware comparison (`claimed = claim exists AND
matches the expected content`, not merely `claim exists`) is what makes this distinguishable from
corroboration. A weaker reconciler that only checked path existence would have misclassified this
case as corroborated — it did not, because the comparison was built to check the actual claimed
proposition, not merely presence.

Similarly, `unreported.txt` is not a file the reconciler happened not to look for — the root parent
independently listed the *entire* output directory (`outdir.iterdir()`), not merely the paths the
self-report mentioned, which is what makes the unreported write discoverable at all. A ground-truth
collector that iterated over the self-report's own claimed paths (rather than the whole directory)
would have structurally been unable to find this case — it would look like a no-evidence case
instead, silently hiding the unreported activity. This is a real, if easy-to-miss, design
requirement DESIGN.md §5 implies but doesn't spell out in this much detail: an external observer
has to scan for what's actually there, not just check the claims it already has.

The truth-table predicate (`reconcile()` called directly, four times, no delegation involved) and
the real-delegation predicate are deliberately kept separate and both required to pass — the first
proves the function's basic logic is right in isolation; the second proves that logic is actually
being fed genuinely independent inputs from a real execution, not synthetic booleans a test author
could get right by construction while the real data-collection code has a bug. Both had to agree.

## Conclusion

Hypothesis **supported**, 15/15 checks, actually run as root on real Linux (colima), refusal path
confirmed on both non-Linux and non-root hosts first. `reconcile()` and `reconcile_path()` — the
first real code in this repo implementing DESIGN.md §3's four-valued comparison — correctly
classify all four Belnap cases, both in isolation (direct truth-table calls) and against real data
from one real delegated sub-agent execution: a genuine corroboration, a genuine active lie about a
file's content (not merely a missing file), a genuine undisclosed extra effect, and a genuine
absence of both claim and effect. Self-report and ground truth were collected through channels that
never touch each other — the self-report from the child's own inherited stdout fd, ground truth from
the root parent's own independent directory scan after independently confirming the child had
exited — matching DESIGN.md §5's requirement that an external observer stay genuinely external.

## Next steps

- This experiment reconciles individual `(path, content)` propositions one at a time. DESIGN.md §3
  also names stable correlation identifiers (`principal_id`, `agent_id`, `intent_id`,
  `parent_intent_id`, `execution_id`, `pid`, `uid`, `cgroup`) as the join keys across the two
  channels — this experiment used `execution_id` implicitly (one delegation, one self-report, one
  ground-truth scan) but didn't test reconciliation across *multiple* concurrent executions' claims
  and observations needing to be correctly joined by those identifiers rather than conflated. A
  natural combination of 006 (many concurrent delegations) with this experiment's reconciliation
  logic — not built here.
- The self-report in this experiment is read once, after the child has already exited. DESIGN.md §3
  also describes self-report as append-only and tamper-resistant at the OS level (a process can
  extend but never rewrite a prior entry) — not tested here; this experiment's self-report is a
  single JSON blob printed once, not an append-only log.
- `container`/`VM` execution classes remain unexplored, same status 004/005/006 left them.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Command runnable from this doc as-is
- [x] Output artifacts under `lab/out/007/`
- [x] Real root/Linux requirement stated explicitly, and actually run there (refusal path confirmed
      on macOS and colima-without-sudo first)
- [x] No methodology slip this run — stated explicitly rather than left silent
