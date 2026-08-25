# Experiment 002: Delegation through the same gate

**Date:** 2026-08-25
**Status:** complete
**Repo state:** siphonophore @ `f662ce9`
**Environment:** portable (any host with Python 3.x)
**Data:** synthetic — see Procedure

## Context

DESIGN.md SS7 requires a second demonstration beyond 001: "a second intent shape — delegation —
must then be demonstrated reducing to the exact same primitive a tool call does, not a
separately-mediated mechanism." HISTORY.md records that when this was built once before, the
`Decision` schema bound `intent_id:principal_id:permitted` but not `kind` — a genuinely-minted
authorization for one kind of effect could, before that was caught, be relabeled and replayed to
authorize a different kind entirely. This experiment rebuilds delegation fresh and, per the task
brief, deliberately tests the exact failure mode HISTORY.md describes finding the hard way, rather
than assuming last time's fix is inherited for free.

## Hypothesis

Three coupled claims:

1. **Mediated delegation succeeds and reduces to the same primitive.** A `delegate` Intent
   submitted through `Gate.submit()` and passed to `Executor.execute()` causes the Executor to
   construct a fresh `Intent` for the sub-agent's own action and push it through `Gate.submit() ->
   Executor.execute()` again — not perform the sub-agent's effect directly. The sub-agent's effect
   is confirmed by reading the actual file back, and its `principal_id` is the sub-agent's own, not
   the delegator's.
2. **A forged `delegate` Decision is refused**, and no sub-agent effect appears on disk — same
   discipline as 001, applied to the new kind.
3. **A genuinely-minted Decision for one `kind` cannot be relabeled and replayed to authorize a
   different `kind`.** Tested two ways: (a) reusing a real `write_file` Decision's token verbatim
   to authorize a `delegate` submission with a different `intent_id`; (b) mutating a real
   `delegate` Decision's `kind` field in place to `write_file` on the same `intent_id`, token left
   unchanged. Both must fail `Gate.verify()` (not merely be refused by some higher-level check) and
   produce no file.

**Null.** Falsified if: sub-agent delegation is dispatched by a code path that doesn't route
through `Gate.submit()` a second time (e.g., the Executor performs the sub-effect directly instead
of constructing and submitting a new Intent); or any forged delegate Decision is accepted; or
either relabel/mutation variant in (3) verifies as `True` and is accepted by `Executor.execute()`.

## Method

- Python: 3.14, stdlib only (`hmac`, `hashlib`, `secrets`, `dataclasses`, `pathlib`, `tempfile`,
  `uuid`) — self-contained, no import from `001_gate_blocks_unmediated_effects.py` or anywhere else
  (DESIGN.md SS0).
- `Gate._canonical()` binds `intent_id`, `principal_id`, `kind`, `permitted` into the HMAC message
  — `kind` present from the first line of this file's Gate implementation, not added after the
  fact, per the task's explicit instruction to build it in from the start this time.
- `Executor.execute()`'s `delegate` branch constructs `sub_intent = Intent(kind=sub_spec["kind"],
  principal_id=sub_spec["principal_id"], intent_id=str(uuid.uuid4()), payload=...)` — a *new*
  Intent object with a freshly minted `intent_id`, then calls `self._gate.submit(sub_intent)`
  followed by `self.execute(sub_decision, sub_intent)`, recursively reusing the exact same
  `execute()` entry point a top-level `write_file` call would use.
- Relabel test: the real `write_file` Decision's `.token` bytes are reused verbatim, attached to a
  hand-built `Decision(kind="delegate", intent_id=<a different, also-genuine delegate intent's
  id>, ...)`.
- Mutation test: a real `delegate` Decision's `.token` is kept unchanged; only `.kind` is flipped
  from `"delegate"` to `"write_file"` in a newly constructed `Decision` object (dataclasses here
  are frozen, so mutation means constructing a new object with the field changed, not `setattr`).

## Procedure

```bash
cd /Users/shunhonda/dev/siphonophore
python3 lab/002_delegation_through_the_same_gate.py
```

## Results

Full raw output: [`out/002/results.json`](out/002/results.json).

| Predicate | Result |
|---|---|
| Delegate Decision `permitted = True` | True |
| Sub-agent's file exists on disk | True |
| Sub-agent's file content matches request | True |
| Sub-agent's effect attributed to sub-agent's own `principal_id` | True |
| Forged delegate Decision refused | True |
| Forged sub-agent file absent | True |
| Relabel case: `Gate.verify()` returns `False` | True |
| Relabel case: `Executor.execute()` refuses | True |
| Mutation case: `Gate.verify()` returns `False` | True |
| Mutation case: `Executor.execute()` refuses | True |
| Relabel/mutation target file absent | True |

Console tail:

```
[PASS] delegate decision permitted
[PASS] sub-agent file exists
[PASS] sub-agent file content matches
[PASS] sub-agent effect attributed to sub-agent principal
[PASS] forged delegate decision refused
[PASS] forged sub-agent file absent
[PASS] relabel: Gate.verify() returns False
[PASS] relabel: Executor refuses
[PASS] mutation: Gate.verify() returns False
[PASS] mutation: Executor refuses
[PASS] relabel/mutation target file absent
HYPOTHESIS SUPPORTED
```

Script exit code: `0`.

## Analysis

All three predicates held on the first run — no relabel/mutation gap surfaced this time, because
`kind` was in the canonical HMAC message from the first line of `Gate._canonical()`, written before
any of the delegation logic existed. This is a direct test of whether building the binding in from
the start (rather than retrofitting it after discovering the gap, as HISTORY.md describes) actually
prevents the specific failure: it does, and the experiment shows *why* mechanically rather than
just asserting it — `gate_verify_result: false` for both the relabel and mutation cases means the
recomputed HMAC (which includes `kind` in its input) genuinely diverges from the reused token the
moment `kind` differs from what was originally signed, independent of any `permitted` or
higher-level policy check.

One thing worth being precise about, since it's easy to state this predicate sloppily: the relabel
case doesn't test "same token, same everything, different kind" in isolation — it necessarily also
changes `intent_id` (the write_file Decision's token was minted for a specific write_file
`intent_id`; reusing it against a delegate Intent requires picking some `intent_id` for that
delegate Intent, and the only realistic attack is pairing the stolen token with a *real* delegate
submission's `intent_id`, which is what the script does). The mutation case is the cleaner isolated
test of "same intent_id, same token, only `kind` changed" and is arguably the stronger of the two —
both fail for the same underlying reason (the HMAC input differs), so this isn't a gap in the
proof, but it's worth naming so a future reader doesn't assume the two cases are testing perfectly
orthogonal things.

The `Executor.execute()` recursion in the `delegate` branch (`self.execute(sub_decision,
sub_intent)`) is doing real double duty here: it's both what makes delegation "reduce to the same
primitive" (literally the same function, not a parallel code path) and what would make a
multi-level delegation chain (sub-agent delegates further) fall out for free without extra code —
not tested here since it's outside 002's scope, but worth flagging as a natural 003+/future
extension rather than a gap.

No methodology slips this round.

## Conclusion

Hypothesis **supported** (3/3 predicates, 11/11 individual checks). Delegation is not a
separately-mediated mechanism — the same `Executor.execute()` entry point handles both kinds, and a
`delegate` Decision's only special behavior is constructing and re-submitting a fresh Intent
through the identical Gate path. Binding `kind` into the HMAC from the first line of this
experiment's Gate implementation prevented the exact relabel/replay gap HISTORY.md documents
finding only after building the naive version once already — confirmed directly, not assumed,
via both a token-reuse and an in-place-mutation variant.

## Next steps

- **003**: add `consequence` on `Intent` and `execution_class` on `Decision`, with the same
  from-the-start binding discipline validated here — DESIGN.md SS2 already states the resulting
  standing rule ("every field Executor branches on must be bound"); 003 is the second empirical
  test of it, on a genuinely new field rather than `kind` again.
- Multi-level delegation (a sub-agent itself delegating further) is a natural extension of this
  experiment's recursive `execute()` call but wasn't tested here — TBD, not yet a numbered
  experiment.

## Reproducibility checklist

- [x] Commit SHA recorded (`f662ce9`)
- [x] Command runnable from this doc as-is
- [x] Output artifacts under `out/002/`
- [x] No one-shot patches or env vars needed
- [x] No methodology slip this round — noted explicitly in Analysis
