# siphonophore — history and lessons learned

`DESIGN.md` states the design as it stands now, with no narrative. This file holds how it got
there and what was learned along the way — real findings worth not re-discovering, not a changelog.

## Origin

Surfaced as a tangent inside the sibling project `warrant`'s own STATUS.md: Strands agent
delegation shares one uid/pid across a parent agent and every sub-agent it delegates to, with no
OS-level way to attribute a sub-agent's actions independently of its parent. Pulled into its own
repo because the fix is general Strands-harness infrastructure, not specific to warrant.

## v1: a delegation-specific orchestrator (built, then superseded)

First design: a `strands.multiagent.base.MultiAgentBase` implementation (`Colony`) sitting
*alongside* Strands, giving only sub-agents flagged for isolation ("severed" nodes) real OS
identity — a dedicated uid, a cgroup, a nonce-based check-in protocol verified via the kernel's
`SO_PEERCRED`, modeled on a Kerberos-shaped two-factor proof (a broker-held nonce plus an
independently kernel-verified peer uid, neither alone sufficient). Non-severed nodes stayed exactly
as Strands already dispatched them.

Built and validated for real on colima (a real Linux VM, root-capable). Two real bugs were found
specifically *because* validation happened on real infrastructure rather than being assumed from
code review alone:

- The check-in Unix socket was created with the broker's own default permissions (0755) — a
  provisioned node's uid, neither the socket's owner nor in its group, had no write permission to
  connect to it at all. Every severed node's check-in was silently, permanently unreachable.
- The check-in nonce traveled via argv, on the reasoning that argv was safer than an env var.
  Checked directly against a real host rather than assumed: `/proc/<pid>/cmdline` is world-readable
  (mode 0444) for a process's entire lifetime; `/proc/<pid>/environ` is actually the more protected
  of the two (owner-uid-only). The nonce was exposed to any local process, the opposite of the
  intended protection. Fixed by passing it through an inherited pipe file descriptor instead —
  readable by nothing outside that pipe's own two ends, regardless of uid.

A retroactive trust-boundary audit (applying Elad Meged's "Trusted Enough to Run" method — list
what's marked safe, find what consumes it with more authority than it earns, find the unchecked
handoff) also found a security claim that had never actually been tested: that a provisioned node's
uid lacks write access to its own cgroup's control files. Verified directly by running a real
subprocess as the provisioned uid and attempting both an escape (moving itself to the root cgroup)
and a manipulation (writing an arbitrary pid to its own cgroup's `cgroup.procs`) — both correctly
refused with `PermissionError`. The claim held, but had been an assumption, not a checked fact,
until specifically tested.

## Why v1 was superseded, not just extended

A conversation surfaced that v1 was answering a narrower question than the one that actually
mattered: "how do we retrofit process isolation onto Strands' existing extension point," not "what
does an agent harness look like if it's designed for attribution and audit from the start."

Two specific realizations drove the redesign:

1. **Strands treats tool calls and delegation asymmetrically**, and that asymmetry — not delegation
   itself — is the actual root of the same-process attribution gap. A harness that applies
   mediation to tool calls but not to delegation reintroduces the same gap one layer up regardless
   of how the delegation-specific piece is isolated. Confirmed directly against Strands' installed
   source, not assumed: `Agent.as_tool()` (`agent/_agent_as_tool.py`) is real and does let a
   sub-agent appear in a parent's `tools=[...]` registry — but `_AgentAsTool.stream()` still just
   calls `self._agent.stream_async(prompt)` in-process. Strands unified the *calling syntax*; it
   did nothing to the *trust boundary*. That distinction — invocation vs. authority — became the
   basis for §1's design.
2. **The octopus/colony framing**: Strands is accurately one organism (one process) with many
   tentacles, including delegation, which is not a flaw relative to what Strands is *for* (a
   general-purpose SDK for building agents quickly, not a security framework). Blanket "every
   sub-agent gets its own OS identity" over-corrects; most agents in a real deployment should stay
   part of a shared process. What actually needs severing is a function of authority width×lifetime
   and untrusted-input exposure, not a fixed rule — the basis for §2's per-intent execution class.

A separate design note (authored by ChatGPT, reviewing an earlier revision of `DESIGN.md`) proposed
reframing the project from "harness" to "SDK for constructing mediated harnesses" and initially
kept Strands as the reference cognitive-runtime dependency. On review, that Strands-dependency
framing was corrected: the actual conclusion reached was no Strands dependency anywhere in
`siphonophore-core` or the default harness — Strands studied as a reference, never imported. §0
reflects that corrected conclusion.

## The no-dependencies principle was violated once, in practice, and the fix was to delete v1's
## code entirely rather than patch around it

While building a uid+cgroup execution-class experiment, v1's `identity.py` (a file with no
external imports of its own) was reused via a path-hack import from an `archive/` copy of v1's
code. That import initially failed because importing it through the package (`from siphonophore
import identity`) executed the package's own `__init__.py`, which still imported
`orchestrator.py`, which imported `strands` — pulling in exactly the dependency the redesign had
just dropped, transitively, even though the specific file being reused had nothing to do with it.

The import mechanism was fixed (loading `identity.py` as a standalone module, bypassing the
package entirely) — but that fix missed the actual issue: reusing *any* code from v1 at all, no
matter how cleanly imported, was itself a violation of "no dependencies." The principle was never
"no `strands` package specifically" — it was "siphonophore stands alone," which includes not
depending on the project's own discarded prior architecture. The entire archived v1 codebase was
deleted as a result, not kept around as reusable reference material. A uid+cgroup execution class
remains valid future work; it has to be built fresh.

## Experiment-driven build process (lab/), findings before the lab/ directory itself was cleared

Building bottom-up via numbered, falsifiable experiments (modeled on an established pattern from
other projects by the same author — paired `.py` reproducer + `.md` write-up, explicit null
hypothesis, honest analysis including methodology slips, real root/Linux claims actually run on
colima rather than assumed portable). Four experiments were run; their code and write-ups were
later cleared from the repository, but the findings are real and worth keeping:

- **Gate blocks unmediated effects** (portable, no root needed): a minimal `Intent → Gate.submit()
  → Decision → Executor.execute()` pipeline, where `Executor.execute()` requires a `Decision` that
  only `Gate.submit()` can validly mint (an HMAC keyed by a secret that never leaves the Gate). A
  mediated effect succeeded and was confirmed on disk; a hand-forged `Decision` was refused, and no
  file was written. First structural proof of §1's central claim, for the smallest possible case.
- **Delegation through the same gate** (portable): added delegation as a second intent kind through
  the identical pipeline. Confirmed delegation reduces to the same primitive a tool call does — and
  surfaced a real gap doing it: the `Decision` schema bound `intent_id:principal_id:permitted` but
  not `kind`, meaning a genuinely-minted authorization for one kind of effect could, before this was
  caught, have been relabeled and replayed to authorize a different kind entirely. Fixed by binding
  `kind` into the token; confirmed the fix directly (the identical token verifies true for its real
  kind, false when relabeled), not just via the pass/fail outcome.
- **Execution class selection** (portable): extended `Decision` with `execution_class`
  (`same_process` | `separate_process`), with real, distinct process IDs as ground truth that the
  class actually changed where an effect ran, not merely what it was labeled. The exact same
  binding gap as the delegation experiment appeared again with the new field, confirming this is a
  general pattern, not a one-off: **any field execution dispatch branches on must be bound into the
  authorization from the moment it's added** — now stated as a standing rule in `DESIGN.md` §2,
  found empirically by hitting the same class of bug twice before generalizing it.
- **uid+cgroup execution class** — retracted, not because the mechanics didn't work (they did, and
  were validated for real on colima: a genuinely distinct provisioned uid, confirmed cgroup
  membership while running, clean teardown, both a forged-Decision and a downgrade-replay bypass
  correctly blocked), but because it was built by importing from the archived v1 codebase — the
  violation described above. The finding that stands: uid+cgroup as a third execution class is
  buildable and testable with the same methodology as the first three experiments; it needs
  rebuilding without depending on anything outside `siphonophore-core`'s own tree.
