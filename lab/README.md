# siphonophore lab notebook

Claim-anchored experiments building `siphonophore-core` bottom-up (DESIGN.md SS6: define core
contracts, implement minimum SDK, build a tiny reference harness proving the central invariant,
discover which abstractions were wrong, refine, expand). Each numbered pair is one experiment:
hypothesis (with an explicit null), method, procedure, real results, honest analysis, conclusion.
Modeled on `~/dev/experiments/substrate-prototype/lab/` and `~/dev/experiments/e-forge/lab/`.

Every script is self-contained and imports nothing beyond the Python standard library — no
dependency on any other file in this repo, any other repo, any installed package, or this
project's own git history (DESIGN.md SS0; see HISTORY.md's account of the one time that principle
was violated, and why the fix was deletion rather than a cleaner import).

## Index

| # | Date | Title | Headline finding |
|---|---|---|---|
| 001 | 2026-08-25 | [Gate blocks unmediated effects](001-gate-blocks-unmediated-effects.md) | Hypothesis **supported** (6/6 predicates). Minimal `Intent -> Gate.submit() -> Decision -> Executor.execute()` pipeline: a mediated write's on-disk content is confirmed by reading the file back; two independent Decision-forgery strategies (made-up token, attacker's own HMAC secret) are both refused by `Executor.execute()`'s own verification, with the target file confirmed absent. Named an open trust boundary: payload fields are not yet bound into the token, only `intent_id`/`principal_id`/`kind`/`permitted` are. |
| 002 | 2026-08-25 | [Delegation through the same gate](002-delegation-through-the-same-gate.md) | Hypothesis **supported** (3/3 predicates, 11/11 checks). `delegate` added as a second `Intent.kind`; `Executor.execute()` handles it by constructing a *new* Intent for the sub-agent's own effect and submitting it through the same Gate — no shortcut. Sub-agent's effect confirmed on disk, attributed to its own `principal_id`. Forged delegate Decision refused. `kind` was bound into the HMAC from the first line this time; both a token-reuse relabel and an in-place `kind` mutation on a genuine Decision correctly failed `Gate.verify()` and were refused — the exact gap HISTORY.md describes finding only after the fact, this time built in from the start. |
| 003 | 2026-08-25 | [Execution class selection](003-execution-class-selection.md) | Hypothesis **supported** (4/4 predicates, 11/11 checks). `consequence` on `Intent`, `execution_class` (`same_process`\|`separate_process`) on `Decision`. Real, distinct PIDs prove the class changes *where* the effect runs: same_process effect's pid == Executor's own pid (79144); separate_process effect's pid (79145) self-reported by a real spawned `subprocess.run` child via its own stdout, not assumed by the parent. `execution_class` bound into the HMAC from the start; a downgrade-replay of a genuine `separate_process` Decision to `same_process` correctly failed verification and was refused — second independent confirmation (after 002's `kind`) of the DESIGN.md SS2 standing rule. |

## Conventions

- **Numbering**: zero-padded three digits, append-only. Once assigned, an ID does not change.
- **Repo state**: every experiment records the siphonophore commit SHA at run time.
- **Environment**: every experiment states whether it's portable (any host, no root) or requires
  real root on real Linux (colima) — and, for the latter, records that it was actually run there,
  not assumed portable from code review alone (HISTORY.md's own lesson from v1).
- **Hypothesis discipline**: every experiment names a falsifiable claim with specific, checkable
  predicates and an explicit null. "It works end-to-end" is not a hypothesis.
- **Analysis is honest**: methodology slips, gaps found while writing, and partial confirmations
  get written up the same as clean wins — see 001's Analysis for an example (a trust-boundary gap
  named, not silently patched, on the same day it was found).
- **Out-of-scope**: pure engineering work (refactors, dependency bumps — there are none, by
  design) belongs in commit messages, not here.

## Template

[TEMPLATE.md](TEMPLATE.md). Copy, fill in, commit.
