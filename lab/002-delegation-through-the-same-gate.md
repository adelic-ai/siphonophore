# Experiment 002: Delegation through the same gate

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `354cb8136fc84a95e997a94b7572225b7833e467` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

Extending lab/001's `Intent → Gate.submit() → Decision → Executor.execute()` shape with a second
intent kind, `delegate`: (1) a mediated delegation succeeds and the delegated sub-agent's own
effect is independently confirmed on disk; (2) a hand-forged `delegate` Decision is refused, same
as lab/001's bypass proof; (3) a genuinely-minted `file_write` Decision cannot be relabeled and
replayed to authorize a `delegate` effect, or vice versa.

**Null:** if either bypass (forged, or cross-kind replay) succeeds in producing an effect, or if
the delegated sub-agent's action can occur without itself passing through `Gate.submit()`,
`DESIGN.md` section 1's unification claim ("tool call and delegation are the same kind of thing
through the same mediation") is falsified — delegation would still be a special-cased shortcut, just
one layer further down than lab/001 checked.

Claim (3) specifically wasn't testable with lab/001's shape at all — with only one intent kind,
there was nothing to replay a token *as*. It's new to this experiment, not an afterthought: adding
a second kind is exactly the situation where an under-specified Decision (bound to
`intent_id:principal_id:permitted` but not `kind`) would have quietly become exploitable, and
lab/001 alone gave no reason to notice.

## Method

- Python 3.14, macOS, no root/Linux required — portable claim, same as lab/001.
- `Decision` gained a `kind` field since lab/001, bound into the HMAC message
  (`intent_id:principal_id:kind:permitted`) specifically so claim (3) is testable and refused.
- `Executor.execute()` on a `delegate` Decision constructs a *new* `Intent` for the sub-agent's own
  action and submits it through the same `Gate` instance the `Executor` already holds — there is no
  separate "delegated executor" with different rules.

## Procedure

```bash
cd ~/dev/siphonophore
python3 lab/002_delegation_through_the_same_gate.py
```

## Results

```json
{
  "mediated_delegation": {
    "decision_permitted": true,
    "executor_raised": null,
    "ground_truth_subagent_file_exists_with_expected_content": true
  },
  "forged_delegate_bypass": {
    "executor_raised": "GateBypassError: decision 'd847741a-cfa3-48b4-a1b7-6c9254176d76' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  },
  "cross_kind_replay": {
    "executor_raised": "GateBypassError: decision 'e38cd9f8-2f13-4749-a3e4-99996d022d50' did not come from a real Gate -- effect refused",
    "ground_truth_file_exists": false
  }
}
```

Exit code: `0` (all three hold).

Checked the mechanism directly, not just the outcome, since a passing replay test could in
principle pass by accident (e.g. an unrelated bug that happens to raise for both attempts):

```
legit file_write decision verifies as file_write: True
same token relabeled to delegate verifies: False
```

The identical token verifies against its real kind and fails against a relabeled one — confirming
`kind` binding is doing the actual work, not coincidence.

## Analysis

The delegation path itself (attempt 1) worked on the first pass, reusing lab/001's `execute()`
recursion cleanly — the sub-agent's file write really did go through a second `Gate.submit()` call,
visible in the code path (`Executor.execute` calling itself with a freshly-minted `sub_decision`),
not just asserted by the write-up.

Claim (3) is the actual finding worth having checked rather than assumed. lab/001's `Decision`
schema (`intent_id`, `principal_id`, `permitted`, `token`) was sufficient for one intent kind and
would have been silently insufficient the moment a second kind was added, if this experiment hadn't
specifically gone looking for it — the token would have verified fine (same `intent_id`, same
`principal_id`, same `permitted`), and a real, legitimately-obtained authorization to write one
specific file could have been replayed to authorize an entirely different class of effect
(delegation). This is a direct instance of `DESIGN.md` section 4's Trusted Enough to Run pillar:
found by asking "what does this Decision's trust actually cover" before extending the schema, not
after something downstream consumed it with more authority than it earned.

No other methodology slip caught this run.

## Conclusion

Hypothesis supported on all three counts. Delegation reduces to the same `Gate`/`Decision`/
`Executor` primitive a tool call does — not a separate mechanism, and not a shortcut a sub-agent's
own actions could route around. The schema itself had to grow (kind-binding) to make that claim
actually hold under a second intent kind, which is the more interesting result than "delegation
worked."

## Next steps

- **003 (TBD):** real execution-class selection (`DESIGN.md` section 2) — both experiments so far
  run every effect in-process. Nothing yet decides same-process vs. separate-process vs. uid+cgroup
  based on the intent's own required authority/consequence.
- **TBD, needs colima/root:** reuse the archived v1 `identity.py`/`checkin.py` as a real execution
  backend and re-run both the forged-Decision and cross-kind-replay methodology against a real
  process/uid boundary — a materially stronger, and still unproven, claim.
- Worth flagging for the eventual real `Policy`/`Authority` layer: this experiment's `_policy()`
  permits every `delegate` intent unconditionally. A real deployment needs the delegate target's own
  scope checked, not just "delegation is allowed in general" — not tested here, out of scope for
  this narrow structural claim.

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is
- [x] Output artifacts under `lab/out/002/`
- [x] Real root/Linux requirement stated explicitly — none needed, noted above
- [x] If a methodology slip was caught: documented in Analysis (the kind-binding gap, found before
      it shipped, not after)
