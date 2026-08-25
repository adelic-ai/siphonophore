# Experiment NNN: Short descriptive title

**Date:** YYYY-MM-DD
**Status:** in-progress | complete | superseded
**Repo state:**
  - `siphonophore` @ `<sha>`

## Hypothesis

State the falsifiable claim. One or two sentences. Avoid "it works end-to-end" — name a specific
predicted outcome. For siphonophore's own lab, this is almost always a claim about mediation or
attribution: what specific effect must be reachable only one way, or what specific fact must be
independently observable.

Name the **null** explicitly: the specific observation that would falsify the hypothesis.

## Method

Environment, configuration. Enough that someone reading this in six months can recreate the
conditions without guessing.

- Python: 3.x
- Whether real root/Linux was required (colima) or the claim is portable — record which; per
  DESIGN.md's own Trusted Enough to Run discipline, a claim that needed root and was only checked
  on the Mac is not actually verified, it's assumed.

## Procedure

Numbered reproducible steps.

```bash
cd ~/dev/siphonophore
.venv/bin/python lab/NNN_title.py
```

## Results

Specific outcomes — did the mediated path succeed, did the unmediated/bypass attempt fail, what
exception (if any) was raised, what ground-truth artifact (if any) was independently checked.

## Analysis

What do the results mean? Where does the result agree or disagree with the hypothesis? If a
methodology slip was caught mid-experiment (the bypass attempt wasn't actually a real bypass
attempt; the check only ran on the Mac when it needed root; etc.), document it here honestly —
that's information, not embarrassment.

## Conclusion

One paragraph: claim confirmed / refuted / partially supported.

## Next steps

What this experiment points at — open questions, follow-up experiments, design changes to
`DESIGN.md` implied. Each item ideally maps to a concrete experiment ID or "TBD".

## Reproducibility checklist

- [ ] Commit SHA recorded
- [ ] Commands runnable from this doc as-is
- [ ] Output artifacts under `lab/out/NNN/` or referenced by absolute path
- [ ] Real root/Linux requirement stated explicitly if applicable, and actually run there if so —
      not just written and assumed
- [ ] If a methodology slip was caught: documented in Analysis
