# siphonophore lab notebook

Claim-anchored experiments proving (or falsifying) specific pieces of `DESIGN.md`. Modeled on
`~/dev/experiments/substrate-prototype/lab/` and `~/dev/experiments/e-forge/lab/`.

Each experiment names a falsifiable claim with an explicit null, runs a real script, and reports
what actually happened — including when it didn't hold. Pure engineering work with no falsifiable
claim (refactors, dependency bumps, doc fixes) belongs in commit messages, not here.

## Index

| # | Date | Title | Headline finding |
|---|---|---|---|
| 001 | 2026-08-25 | [Gate blocks unmediated effects](001-gate-blocks-unmediated-effects.md) | Hypothesis **supported**. Minimal `Intent → Gate.submit() → Decision → Executor.execute()` pipeline: a mediated file-write succeeds and is confirmed on disk (ground truth, not just a clean return value); a hand-forged `Decision` (never minted by `Gate.submit()`) is refused by `Executor.execute()`'s own HMAC verification, and no file is written. Structural proof for the smallest possible case — not yet covering delegation, execution-class selection, or real OS-level (uid/cgroup) enforcement. |
| 002 | 2026-08-25 | [Delegation through the same gate](002-delegation-through-the-same-gate.md) | Hypothesis **supported** (3/3). Delegation added as a second intent kind, reusing the same `Gate`/`Executor`: mediated delegation succeeds with the sub-agent's own effect confirmed on disk; a forged `delegate` Decision is refused; a *genuinely-minted* `file_write` Decision cannot be relabeled and replayed to authorize a `delegate` effect. **Real finding**: lab/001's `Decision` schema didn't bind `kind`, which would have been silently exploitable the moment a second intent kind existed — caught by specifically testing for it, not by accident. |
| 003 | 2026-08-25 | [Execution class selection](003-execution-class-selection.md) | Hypothesis **supported** (4/4). Added `consequence` on `Intent`, `execution_class` on `Decision` (`same_process` / `separate_process`, real distinct pids confirmed as ground truth — not a label nothing consults). Forged Decision refused; a genuinely-minted `separate_process` grant cannot be downgraded to `same_process` and replayed. **Real finding**: the same per-field-binding gap as lab/002, now seen twice — generalized into a standing rule (any field `Executor.execute()` branches on must be bound into the token from the moment it's added). |
| 004 | 2026-08-25 | [uid+cgroup execution class](004-uid-cgroup-execution-class.md) | Hypothesis **supported** (3/3), **real root on real Linux (colima), not portable**. Third execution class, `uid_cgroup`, backed by the archived v1 `identity.py` (real `useradd`/cgroupfs, reused not reimplemented). Effect ran under a genuinely distinct provisioned uid (`59001`, broker at `0`), confirmed in-cgroup while running, cleanly torn down after. Forged and downgrade-replay bypasses both blocked — no new `Decision` field needed, confirming lab/003's binding rule was actually sufficient. **Real finding**: the archived package's `__init__.py` still transitively imports `strands` — caught and fixed (loaded `identity.py` standalone via `importlib.util`) before the real run, a live instance of the DESIGN.md section 0 decision mattering in practice, not just in principle. |

## Conventions

- **Numbering**: zero-padded three digits, append-only.
- **Repo state**: every experiment records the `siphonophore` commit SHA it ran against.
- **Real substrate discipline**: if a claim needs real root/Linux to mean anything (execution-class
  separation, cgroup enforcement, anything reusing the archived v1 primitives), that requirement is
  stated explicitly and the experiment is actually run there (colima), not assumed portable because
  it happened to run on the Mac. This is `DESIGN.md` section 4's Trusted Enough to Run pillar
  applied to the lab process itself.
- **Hypothesis discipline**: every experiment names a falsifiable claim, not "it works end-to-end."
  Name the null.
- **Analysis is honest**: methodology slips and falsified hypotheses get written up the same as
  confirmations.

## Template

[TEMPLATE.md](TEMPLATE.md). Copy, fill in, commit.
