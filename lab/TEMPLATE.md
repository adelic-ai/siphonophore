# Experiment NNN: Short descriptive title

**Date:** YYYY-MM-DD
**Status:** in-progress | complete | superseded
**Repo state:** siphonophore @ `<commit-sha>`
**Environment:** portable (any host with Python 3.x) | real root on Linux (colima) — state which, and why
**Data:** synthetic — see Procedure

## Hypothesis

State the falsifiable claim being tested. One or two sentences. Avoid "it works end-to-end" —
name the specific predicted outcome (e.g. "a hand-forged Decision is refused by
`Executor.execute()`'s own verification, and the target file is confirmed absent afterward").

Name the **null** explicitly: the specific observation that would falsify the hypothesis.

## Method

Environment, configuration, parameters. Enough that someone reading this in six months can
recreate the conditions without guessing.

- Python: 3.x (stdlib only — no external dependencies, per DESIGN.md §0)
- Parameters: secret/key handling, HMAC fields bound, number of trials, ...
- Root/Linux requirement (experiment 4 only): what's checked at startup, what happens if absent

## Procedure

Numbered reproducible steps. Commands, file paths, env vars.

```bash
cd /Users/shunhonda/dev/siphonophore
python3 lab/NNN_<title>.py
```

## Results

Tables, real numbers, raw outputs — ground truth (file contents read back, real PIDs, real uids,
real cgroup membership), not just exit codes. Reference output artifacts under `out/NNN/`.

## Analysis

What do the results mean? Why do they look like they do? Where does the result agree or disagree
with the hypothesis? What is the most surprising finding?

If a methodology slip was caught mid-experiment, document it here — honestly, not silently fixed.

## Conclusion

One paragraph: claim confirmed / refuted / partially supported. The single sentence the next
reader needs.

## Next steps

What this experiment points at — open questions, follow-up tests, design changes implied. Each
item ideally maps to either a concrete experiment ID (if a follow-up has happened) or "TBD".

## Reproducibility checklist

- [ ] Commit SHA recorded
- [ ] Command runnable from this doc as-is
- [ ] Output artifacts under `out/NNN/` or referenced by absolute path
- [ ] Any one-shot patches or env vars documented
- [ ] If a methodology slip was caught: documented in Analysis
- [ ] (Experiment 4 only) Confirmed actually run as root on real Linux (colima), not assumed
