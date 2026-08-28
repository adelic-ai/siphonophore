# Independent evidence and reconciliation

Agent self-report and machine observation are intentionally separate evidence channels in
Siphonophore. An agent may claim `"I performed effect X"` while an independent execution boundary
observes `principal B performed machine effect X`. Neither observation is silently substituted for
the other, and neither is treated as confirming the other by default.

## Belnap four-valued reconciliation

Collapsing "the agent lied" and "the agent did something it never mentioned" into a single
true/false match would throw away exactly the distinction that matters for attribution. `audit.py`
instead uses [Belnap four-valued logic](https://en.wikipedia.org/wiki/Four-valued_logic) — true,
false, both, neither — to keep four real states distinct:

| Agent claim | OS observation | State |
|---|---|---|
| yes | yes | corroborated |
| yes | no | contradiction |
| no | yes | unreported activity |
| no | no | no evidence |

The purpose isn't to make an agent's own transcript irrelevant — it's still useful for
explainability. It's to avoid treating an agent's description of its own behavior as independent
evidence of that behavior. A genuinely authentic, kernel-verified execution identity whose
self-report lies about what it did still reconciles as `contradiction` or `unreported_activity`,
never `corroborated` — a real identity plus a false claim is still refused as confirmation.
`tests/test_harness_loop_linux.py` demonstrates this directly, composed with real delegated
authority and the unprivileged `siphonophore-spawn` execution path.

## Execution identity is not the same claim as logical agent identity

Ground truth in this model is established by an observer outside the agent's own trust domain —
process/uid/cgroup accounting, or a check-in protocol verified by the kernel (`SO_PEERCRED`) rather
than asserted by the process being verified. That's real, meaningful independence: it isn't the same
process vouching for itself.

It is a narrower claim than "this specific logical agent did this," though, and worth stating
precisely. What a kernel-verified check-in actually establishes is *which process* performed an
effect — not automatically *which logical agent*, in whatever sense a harness above Siphonophore
defines agent identity. The two coincide exactly when policy has assigned that process a distinct
execution identity for that action (see [`EXECUTION.md`](EXECUTION.md) — a real design choice, not a
default). Where an execution class doesn't provision a distinct UID or cgroup for a given intent —
`same_process`, most concretely — ground truth is correspondingly coarser: it can establish that the
broker's own process performed an effect, not that one specific logical agent among several sharing
that process did, independent of what that agent claims. Attribution at agent granularity is exactly
as strong as the execution identity that policy chose to provision for that action, never stronger
than that by default.

## Where this composes with the rest of the architecture

Reconciliation happens above both claims, joined by stable correlation identifiers (`principal_id`,
`intent_id`, `execution_id`, `pid`, `uid`, `cgroup`) produced independently by each channel — never
inside either claim itself. `CheckedInSpawnHelperBackend` composes this with delegated authority and
the unprivileged-broker execution path with no changes required to `siphonophore-spawn.c` or the
pinned spawn-helper contract — the check-in channel (`SH-09`/`SH-24`) was already defined, just not
previously exercised. See `DESIGN.md` §3 for the underlying design rule this implements, and §9's
"Explicitly open" notes for what reconciliation doesn't yet handle (e.g. richer-than-boolean claims,
or a check-in failure co-occurring with an already-performed effect on a differently-shaped backend).
