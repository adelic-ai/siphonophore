# Experiment 001: Gate blocks unmediated effects

**Date:** 2026-08-25
**Status:** complete
**Repo state:** siphonophore @ `b43a145`
**Environment:** portable (any host with Python 3.x)
**Data:** synthetic — see Procedure

## Context

DESIGN.md SS1 and SS7 state the central invariant this whole project exists to prove: the
cognitive loop must be **structurally unable** to produce an effect except through the Gate. This
is the smallest possible vertical slice of that claim — one Intent kind (`write_file`), one Gate,
one Executor — built to find out whether an HMAC-token design for `Decision` actually enforces
that, or only appears to on the happy path.

## Hypothesis

A minimal `Intent -> Gate.submit() -> Decision -> Executor.execute()` pipeline enforces mediation
two ways simultaneously:

1. A mediated file-write (Intent submitted through `Gate.submit()`, resulting `Decision` passed to
   `Executor.execute()`) succeeds, and the file's actual on-disk content, read back independently
   of the Executor's own return value, matches what was requested.
2. A `Decision` object constructed directly — bypassing `Gate.submit()` entirely, whether via a
   made-up token string or a token computed with an attacker-chosen secret the real Gate never
   used — is refused by `Executor.execute()`'s own verification (not merely a policy check on
   `.permitted`), and the target file is confirmed **absent** afterward, not just "no exception
   raised by the happy path."

**Null.** The hypothesis is falsified if either: (a) the mediated write's on-disk content fails to
match what was requested (Executor claims success without a real effect, or a wrong effect), or
(b) any forged Decision variant is accepted by `Executor.execute()` and produces a file on disk —
i.e., `Executor.execute()` trusts `decision.permitted` or the caller's say-so rather than
independently re-deriving and checking the HMAC token against the Gate's own secret.

## Method

- Python: 3.14 (host), stdlib only (`hmac`, `hashlib`, `secrets`, `dataclasses`, `pathlib`,
  `tempfile`, `uuid`) — no dependency outside this file, per DESIGN.md SS0.
- `Gate` holds a 32-byte random secret (`secrets.token_bytes(32)`) generated in `__init__`, stored
  as `self._secret`, with no public accessor. The class's entire public surface is asserted at
  runtime (`gate_public_surface` in results) to be exactly `["submit", "verify"]`.
- The HMAC message binds every field `Executor.execute()` inspects: `intent_id`, `principal_id`,
  `kind`, `permitted` — joined as `f"{intent_id}:{principal_id}:{kind}:{permitted}"`, SHA-256 HMAC,
  hex digest.
- `Executor.execute()` never reads `decision.permitted` before calling `gate.verify(decision)`;
  verification happens first and independently re-derives the expected token from the Decision's
  own field values, comparing with `hmac.compare_digest` (constant-time).
- Two forgery variants tested: (1) a syntactically-plausible but arbitrary 64-hex-char token: `"0"
  * 64`; (2) a token minted with a *different*, attacker-chosen 32-byte secret over the identical
  canonical message — simulating an attacker who understands the HMAC scheme completely but never
  had access to the real Gate instance's secret (which, per the design, never leaves it).

## Procedure

```bash
cd /Users/shunhonda/dev/siphonophore
python3 lab/001_gate_blocks_unmediated_effects.py
```

Single run, no randomness affecting pass/fail (the nonce embedded in file content is only for
uniqueness, not compared against anything except the file Executor actually wrote).

## Results

Full raw output: [`out/001/results.json`](out/001/results.json).

| Predicate | Result |
|---|---|
| Mediated Decision has `permitted = True` | True |
| Mediated file exists on disk | True |
| Mediated file content read back matches request | True |
| Forged Decision (made-up token) refused by Executor | True |
| Forged Decision (attacker's own secret) refused by Executor | True |
| Target file for forged intent absent after both attempts | True |
| `Gate`'s public surface | `["submit", "verify"]` (no secret accessor) |

Console tail:

```
[PASS] mediated write permitted
[PASS] mediated file exists
[PASS] mediated file content matches
[PASS] forged variant 1 refused
[PASS] forged variant 2 refused
[PASS] forged file absent
HYPOTHESIS SUPPORTED
```

Script exit code: `0`.

## Analysis

Both forged-Decision variants were refused for the same underlying reason: `Executor.execute()`
recomputes the HMAC from the Decision's own field values using the Gate's secret and compares
against `decision.token`, so any Decision not produced by that exact Gate instance's `_mint()`
fails verification regardless of how plausible the token string looks or how sophisticated the
forger's own signing scheme is. There's nothing subtle in this result — it's the intended
mechanism working as specified.

The more interesting design point that fell out of writing this experiment: `Executor.execute()`
also cross-checks `decision.intent_id` and `decision.kind` against the `Intent` object passed
alongside it, before even reaching Gate verification. This wasn't in the original design sketch —
it surfaced while writing the method because otherwise a caller could pair a validly-minted
Decision for one Intent with an entirely different Intent object at the call site (not a
cryptographic forgery, just object-level confusion at the call site, e.g. a bug rather than an
attack). This experiment doesn't test that path directly, but it's a related trust boundary worth
naming: **`Executor.execute()`'s cross-check that `decision` and `intent` actually refer to the
same submission is currently based on `intent_id` matching, not itself part of the HMAC** — the
Decision's token doesn't bind to a hash of the Intent's full payload, only to `intent_id`,
`principal_id`, `kind`, and `permitted`. Nothing in this experiment's payload (`path`, `content`)
is bound into the token. That is a real gap for a production Gate — a Decision minted for one
`write_file` Intent could in principle be paired at the call site with a *different* `write_file`
Intent sharing the same `intent_id`-independent fields but a different payload, and it would still
verify. This experiment's Executor doesn't currently guard against that because `intent_id` is
assumed unique per Intent and the caller is assumed to pass the same Intent object it submitted —
an assumption, not something enforced. Flagging honestly rather than silently fixing, per
HISTORY.md's own account of how these gaps get found: this is a trust boundary named per DESIGN.md
SS4, not yet closed. A production Gate should likely bind a payload hash into the token too; this
minimal experiment doesn't need it to prove the central invariant, so it's left open rather than
scope-creeping experiment 001.

No methodology slips this round — the mediated-path and forged-path predicates all resolved on the
first run.

## Conclusion

Hypothesis **supported**: a Decision only verifies when it was actually minted by `Gate.submit()`
using the Gate's own never-exposed secret over the fields Executor branches on: a mediated write
produces a real, independently-confirmed file; two independent forgery strategies are both refused
by `Executor.execute()`'s own verification (not a policy check on a trusted field), and produce no
file. First structural proof, at the smallest possible scale, of DESIGN.md SS1's central claim.

## Next steps

- **002**: add `delegate` as a second `Intent.kind`, through the identical Gate/Executor pipeline —
  confirm delegation reduces to the same primitive a tool call does, and bind `kind` into the HMAC
  from the first line of Decision-minting code (already true in 001; carry forward, don't
  reintroduce the gap HISTORY.md documents finding the hard way last time).
- Named trust boundary above (payload not bound into the token) — not closed here; worth revisiting
  once the SDK's real policy engine exists and payload shapes are less ad hoc than this
  experiment's raw dict.

## Reproducibility checklist

- [x] Commit SHA recorded (`b43a145`)
- [x] Command runnable from this doc as-is
- [x] Output artifacts under `out/001/`
- [x] No one-shot patches or env vars needed
- [x] Methodology note documented in Analysis (payload-not-bound trust boundary, found while
      writing, not silently patched)
