# Experiment 008: Execution provenance — does a bound artifact digest actually catch a swap

**Date:** 2026-08-25
**Status:** complete
**Repo state:**
  - `siphonophore` @ `df0ae48ea5aae2789594b34881645c5aefcb582d` (this script and write-up land in
    the commit immediately after this SHA)

## Hypothesis

`DESIGN.md` §8 states execution provenance as a requirement: a provisioned uid/cgroup identifies
*which running process* is being observed, not whether that process is running the code the broker
actually meant to authorize. Adding `artifact_digest` to `Decision`, bound into the HMAC alongside
`kind` and `execution_class` (§2's existing binding discipline): (1) a mediated execution of
authorized code succeeds, with the digest genuinely reflecting what ran; (2) presenting the
Executor with *different* code than what was authorized — same `intent_id`, different
`artifact_code` — is refused by an independent re-hash at execution time, not just by `Gate.verify()`;
(3) a forged Decision is refused before anything runs; (4) a genuine Decision with its
`artifact_digest` field swapped to a *different legitimate-looking* digest (not garbage — a real
digest of a real, different program) fails verification.

**Null:** if a swapped artifact runs anyway — the code that actually executes differs from what a
real `Gate.submit()` call authorized, and nothing catches it — the claim that execution provenance
closes the gap between "what was authorized" and "what ran" is falsified. This is specifically a
time-of-check-to-time-of-use concern: `Gate.verify()` alone confirms a Decision's own fields weren't
tampered with after minting, but says nothing about whether the code hitting `Executor.execute()`
right now is the same code that was hashed at `submit()` time — that's a distinct check, and
predicate (2) exists specifically to prove it's actually being made, not assumed redundant with
`Gate.verify()`.

## Method

- Portable — no root needed. This experiment is about the binding/verification logic itself, kept
  deliberately free of uid/cgroup complexity, the same way `001`-`003` stayed simple before `004`
  introduced privilege separation on purpose. Execution provenance and OS-level identity are
  orthogonal concerns; conflating them in one experiment would make it harder to tell which
  mechanism a given result was actually about.
- `artifact_digest` is `sha256(artifact_code)`, computed once by `Gate.submit()` (binding what was
  authorized) and independently recomputed by `Executor.execute()` immediately before running
  anything (checking what's about to happen). Two separate computations of the same function,
  compared with `hmac.compare_digest` — not one computation trusted twice.

## Procedure

```bash
cd ~/dev/siphonophore
python3 lab/008_execution_provenance.py
```

## Results

```
[PASS] happy path: file content matches program A
[PASS] happy path: digest bound in Decision
[PASS] swapped artifact: ArtifactMismatchError raised
[PASS] swapped artifact: target file absent
[PASS] forged: refused
[PASS] forged: target file absent
[PASS] digest tamper: Gate.verify() returns False
[PASS] digest tamper: Executor refuses
[PASS] digest tamper: target file absent
HYPOTHESIS SUPPORTED
```

The swapped-artifact case (predicate 2) is the one that matters most and is worth showing in full —
a genuine `Decision` for program A, presented at execution time with program B's code instead:

```
artifact digest mismatch: decision authorized 3494f2afdcbf..., but the code about to run hashes to 2b2e162485b5...
```

Full JSON in `lab/out/008/results.json`.

## Analysis

All nine checks passed on the first attempt. Worth being precise about why predicate 2 and
predicate 4 are testing genuinely different things, not the same property twice: predicate 4
(digest-tamper replay) is the same class of test `002` and `003` already ran for `kind` and
`execution_class` — mutate a bound field on a real `Decision`, confirm `Gate.verify()` catches the
mismatch between the token and the field. Predicate 2 (swapped artifact) doesn't touch the
`Decision` at all — the `Decision` is completely genuine and internally consistent; what's swapped
is the *code presented at execution time*, which `Gate.verify()` structurally cannot see, since it
only ever inspects the `Decision` object's own fields. Catching predicate 2 requires the second,
independent re-hash inside `Executor.execute()` — if that check were removed, `Gate.verify()` alone
would return `True` on the swapped-artifact case, because nothing about the `Decision` itself was
touched. This is the actual reason execution provenance needs its own verification step rather than
folding into the existing binding discipline: binding a field into the token proves the *field*
wasn't tampered with; it does nothing to prove the *code handed to the executor* matches what the
field claims, unless something explicitly re-derives and compares.

No methodology slip this run.

## Conclusion

Hypothesis supported on all four predicates, 9/9 checks. Execution provenance, implemented as a
bound digest plus an independent re-hash at the point of execution, catches both classes of attack
it needs to: a swapped artifact presented at execution time (caught by the re-hash, invisible to
`Gate.verify()` alone) and a tampered `Decision` field (caught by `Gate.verify()`, the same
mechanism already proven for `kind` and `execution_class`). `DESIGN.md` §8's execution provenance
requirement is no longer stated-but-unimplemented — this is the first experiment to build it.

## Next steps

- `uid_cgroup` execution class (`004`) does not yet carry `artifact_digest` — worth combining once
  there's a reason to, the same way `005` combined delegation with uid+cgroup only once both were
  independently proven.
- This experiment authorizes a raw code string as the artifact. A real deployment would more likely
  authorize a reference (a module path, a container image digest, a package version) rather than
  inline source — the digest-binding principle is the same either way, but "what exactly gets
  hashed" for a reference-based artifact is a real design question this experiment didn't need to
  answer for an inline string.
- `DESIGN.md`'s broker-integrity question (§8, "who attests the broker's own integrity") remains
  fully open — execution provenance answers "did the broker run the code it meant to," not "is the
  broker itself the expected code."

## Reproducibility checklist

- [x] Commit SHA recorded
- [x] Commands runnable from this doc as-is
- [x] Output artifacts under `lab/out/008/`
- [x] Real root/Linux requirement stated explicitly — none needed, portable by design
- [x] If a methodology slip was caught: none this run — stated explicitly rather than left silent
