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

## lab/001–009, and formalizing them into siphonophore-core + siphonophore-harness

The four retracted experiments above were rebuilt hands-off (dispatched to background agents,
each independently verified afterward rather than trusted on its own report) as `lab/001`–`009`,
this time inside `lab/`'s own stated charter: every script self-contained, stdlib-only, no
dependency on any other file in the repo (`lab/README.md`). Nine experiments, each real-substrate
validated where the claim required it (004–007, 009 actually run as root on colima; 001–003, 008
portable): Gate/Decision binding for `kind`, `execution_class`, and (008/009) `artifact_digest`;
uid+cgroup as a real execution class with clean teardown on every exit path (004, then 005/006
adding a nonce+`SO_PEERCRED` check-in protocol first as a dedicated socket per delegation, then as
one shared listener under genuine concurrency); the first real implementation of §3's Belnap
four-valued reconciliation, exercised against a sub-agent that actually lies (007); execution
provenance as an artifact-digest binding, combined with uid+cgroup (008/009).

Once nine independently-proven experiments existed, `siphonophore-core` was built as the real,
importable, permanently-tested package consolidating them — not a tenth copy of the same shapes,
one formalization pass per lab-proven primitive, each its own `feat/<module>` branch merged to
`main` once its test suite (portable + `linux_root_only`, the latter run for real on colima) was
green: `intent`/`policy`/`mediation` (Intent, Decision, Gate) first; then `execution` +
`execution_uid_cgroup` (`Executor`, the three execution-class backends); then `identity`
(`CheckinRegistry`/`CheckinListener`, lab/005+006's check-in protocol); then `audit`
(`reconcile()`/`reconcile_path()`, lab/007's Belnap logic). `siphonophore-harness` followed as a
fifth pass — the "minimal native cognitive loop" §6 requires — built on top of the now-formalized
core rather than as a tenth lab experiment: `Model`/`ScriptedModel`, `parse_intent` (the only place
untrusted completion text becomes an `Intent`, never a `Decision`), `Broker` (the only
Effect-producing capability a `CognitiveLoop` is given), and `CognitiveLoop` itself.

**§7's proof — the cognitive loop structurally unable to produce an effect except through the
Gate, and delegation reducing to the exact same primitive a tool call does — is demonstrated by
this formalization, not merely asserted.** `test_harness_structural_proof.py` enforces the first
half by static analysis (AST-parsing `loop.py`/`intent_parsing.py`/`model.py`/`broker.py` for a
blocklist of effect-producing stdlib imports, and asserting `CognitiveLoop.__init__` accepts
nothing but a model, a broker, and a principal id) rather than trusting a docstring's claim.
`test_harness_broker.py` and `test_harness_loop_linux.py` prove the second half twice: portably (a
`"delegate"`-kind intent and a `"run_artifact"`-kind intent both landing on `same_process` through
the identical `Broker.dispatch()` call) and for real on colima (both landing on `uid_cgroup`, real
distinct provisioned uids, real privilege drop, zero special-casing for `"delegate"` anywhere in
`loop.py`, `broker.py`, or `execution.py`). One real, if minor, finding along the way: a first
draft of the real-delegation test asserted the two dispatches would get *different* provisioned
uids, which is false for sequential (non-concurrent) calls — the second dispatch legitimately reuses
the first's now-released uid number. Fixed before merge; concurrent-identity separation is already
covered elsewhere (`test_execution_uid_cgroup.py`, `test_identity_linux.py`), so nothing was lost
by removing the wrong assertion.

**An honest gap, closed the same day it was named:** `identity.py` and `audit.py` were validated,
freestanding primitives — proven against real delegated sub-agents on colima — but neither was
wired into any `Executor` backend's actual dispatch path. Closed by
`CheckedInUidCgroupBackend` (`execution_uid_cgroup_checkin.py`): a new `execution_class`,
`"uid_cgroup_checkin"`, registered alongside (not replacing) the original unchecked
`UidCgroupBackend`. The spawned process must pass a real check-in — nonce over an inherited pipe
fd, `SO_PEERCRED` — before anything it did is trusted; a failed or timed-out check-in raises
`identity.IdentityError`, with uid/cgroup released on every exit path including that one. If the
caller supplies `"outdir"` in the intent's payload, the child's stdout is parsed as a self-report
and automatically reconciled against ground truth from that directory — lab/007's four-case logic
(corroborated/contradiction/unreported_activity/no_evidence), now produced automatically rather
than assembled by hand, validated against a real delegate that actively lies about one file and a
real delegate that prints no self-report at all (treated as empty claims, not a hard failure — no
change of policy from `audit.py`'s own "self-report is optional" framing).

Deliberately kept as its own execution class rather than folded into `UidCgroupBackend` or
special-cased on `intent.kind`, per §2 (execution class follows authority, not capability type) and
§7 (delegation is not a separately-mediated mechanism) — a caller that wants unchecked, lab/004-
style trust still has it under `"uid_cgroup"`; check-in-gated trust is an additional, selectable
tier a `Policy` can route to, not a replacement. What this pass did *not* decide: whether
check-in-gated trust should eventually be the default for any delegation rather than an opt-in a
caller has to choose (see DESIGN.md's "Explicitly open" section) — same_process and
separate_process delegations still have no check-in or automatic-reconciliation equivalent at all.

## First real model in the loop: AnthropicAPIModel, and the first live-model bug

Everything up to this point had proven the harness's structural properties against
`ScriptedModel` — deterministic text, never anything a real model would actually produce.
`AnthropicAPIModel` (`model_anthropic.py`) added the first real, network-backed `Model`:
Anthropic's own official SDK, API-key billed rather than subscription/OAuth, deliberately —
the Claude Agent SDK wraps the actual Claude Code CLI, a full local agent runtime with real
tool-execution capability, so unless every tool were correctly disabled (a configuration choice,
not a structural fact) it could perform a local effect entirely underneath `Model.complete()`,
before any Intent exists, invisible to the Gate. A raw API client cannot do that by construction —
the reason this was worth being deliberate about rather than defaulting to whichever billing model
was more convenient.

Getting a real model to speak the intent-JSON protocol at all needed one more piece nothing had
built yet: `DEFAULT_SYSTEM_PROMPT` (`prompts.py`). Without it, a real model has no reason to
respond with JSON instead of ordinary conversation — the very first real turn would fail on
`IntentParseError` before anything interesting happened. `parse_intent` also gained tolerance for
a clean markdown code fence (models commonly wrap JSON in ` ```json ... ``` ` even when told not
to) — a formatting normalization, not a schema relaxation; a malformed or partial fence is left
untouched and still fails normally.

**The first live run against `claude-sonnet-5`, through `examples/repl.py`, found a real bug on
the very first message.** Asked to write a file, the model correctly chose `kind="write_file"`
but left `artifact_code` out — and execution failed with `"same_process backend requires
intent.artifact_code"`. Not a model failure: the system prompt said artifact_code was "required
for run_artifact and delegate," implying write_file might not need it, which is false — no
backend gives `kind` any special handling at all; every effect happens by running code,
regardless of which kind label was chosen. The prompt described a distinction that doesn't exist
in `execution.py`. This is exactly the kind of thing `ScriptedModel`-only testing structurally
cannot find — a scripted completion is never confused by an ambiguity in the instructions, because
it doesn't read them. Fixed in the same session (stating plainly that artifact_code is required
for any intent meant to do something, with an inline example), and re-validated live: the second
real run against `claude-sonnet-5`, same phrasing, produced a well-formed intent with real
`artifact_code`, ran under `same_process`, and the target file's content was confirmed by hand
afterward — the first genuine, non-scripted, end-to-end proof of the whole chain: real model →
`parse_intent` → `Gate` → `Executor` → real effect on disk.

## Real environment-inheritance gap, found by applying an external audit method to this repo directly

Elad Meged's "Trusted Enough to Run" (Black Hat USA 2026) documents the same bug shape recurring
across Claude Code, Gemini CLI, and Codex: one component marks something safe (sanitized,
read-only, pre-approved), and a more powerful component downstream consumes it without
re-checking. His Gemini CLI case — environment sanitization done correctly at the application
layer, while the spawned child still shared the parent's OS-level PID namespace and could read
real secrets straight out of `/proc/$PPID/environ` — was checked directly against
`UidCgroupBackend`/`CheckedInUidCgroupBackend` rather than just read about. Same gap, found for
real: neither backend's `subprocess.Popen()` call passed `env=`, so a spawned child inherited the
broker's *entire* environment by Python's own default behavior, including any real secret the
broker process held (`ANTHROPIC_API_KEY`, running through `examples/repl.py`), despite genuinely
running under a different, unprivileged, kernel-verified uid. The uid boundary was real; the
environment boundary was never built at all. Fixed with `default_child_env()` — an explicit
allowlist (`PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`) computed fresh per execution rather than the
parent's environment filtered after the fact — validated for real on colima: a fake secret set in
the broker's own environment is confirmed absent from a real spawned child's environment, for both
backends, not merely asserted to be filtered.

## The broker-root-privilege gap: same_process/separate_process inheriting whatever the broker has

A question about whether agent-authored code is ever isolated from root surfaced a real gap:
`same_process`/`separate_process` inherit whatever privilege the broker process itself runs with,
with zero resistance — `same_process` does a bare `exec()` in the broker's own process. The broker
has to run as real root for the `uid_cgroup`/`uid_cgroup_checkin` tiers to work at all (`useradd`,
cgroup management, the `setuid` drop all require it), and `Executor` registers
`same_process`/`separate_process` by default, unconditionally. So a broker wanting both kinds of
tier available runs entirely as root — and nothing stopped a "low consequence" intent (the
default, self-declared by the model, not independently verified) from running via `same_process`
with that root broker's full privilege and zero isolation. The weakest isolation tier became the
most dangerous the moment root entered the picture at all.

Fixed the acute, silent part: `SameProcessBackend`/`SeparateProcessBackend` now refuse outright
when the broker is euid 0, unless a caller explicitly passes `allow_root=True` — a silent privilege
inheritance became a loud, impossible-to-miss configuration error. This does not solve the
underlying problem — nothing yet lets one broker run the `uid_cgroup` tiers *and* stay genuinely
unprivileged itself; that needs real deployment-level work (cgroup v2 delegation, a narrowly-scoped
sudo-mediated path for `useradd`/`userdel`) outside what a pure code change can do. Named explicitly
in DESIGN.md's open questions rather than left implied.

## Privilege separation for useradd/userdel — validated for real, one of three pieces

The bigger fix — a broker that can use the `uid_cgroup` tiers without being root itself — needed
naming as three separate problems, not one, since they're not equally hard: cgroup management
(easy, pure delegation via `chown`, no code change), `useradd`/`userdel` (medium, needs scoped
elevation), and the `preexec_fn` uid-drop when spawning the artifact process (hard, the forking
process fundamentally has to already be root to switch to an arbitrary target uid — no
unprivileged workaround identified yet).

Built and validated the middle piece for real. `provision_ephemeral_user()`/
`release_ephemeral_user()` now go through two tiny wrapper scripts
(`scripts/siphonophore-useradd`/`-userdel`), each independently validating the uid range and
username pattern before calling the real binary with fixed flags — nothing about the actual
invocation is caller-configurable. Elevated via `_elevation_prefix()`, mirroring
`warden/privilege.py`'s own pattern exactly (`sudo -n`, never prompt; skipped entirely when already
root).

Proven on colima, not just reasoned through: a genuinely unprivileged test user, a real scoped
sudoers grant installed and validated with `visudo -c`, then `provision_ephemeral_user`/
`release_ephemeral_user` called directly from that unprivileged user — succeeded with zero password
prompt, created and deleted a real system user. Confirmed the scoping is genuinely narrow, not
`ALL`: a raw `useradd` via sudo from the same user was denied outright, not covered by the grant.
Confirmed the wrapper scripts' *own* independent validation, separate from sudo's scoping: an
out-of-range uid and a non-matching username (`root`) were each refused by the script itself before
ever reaching the real `useradd`. Both real attacks tested, not one. Test user and sudoers file
removed afterward; colima confirmed clean.

Real methodology slip, caught the way this project's discipline expects: `sed`'s default per-line
single substitution left one of two `<REPO_PATH>` placeholders unreplaced on a line with two
occurrences, producing a syntactically invalid sudoers file. `visudo -c` — which the install
instructions already required — caught it before anything trusted it; `sudo -n true` was checked
directly and confirmed still working before fixing the file, rather than assuming the failure mode.
Fixed with the `g` flag.

**Still fully open, unchanged:** no unprivileged broker can perform the `preexec_fn` uid-drop step
itself — until that closes, this piece alone does not let a broker run the `uid_cgroup` tiers while
staying unprivileged.

## The spawn-helper contract: frozen, then amended twice by review, before any code existed

The third and hardest piece — the `preexec_fn` uid-drop, which needs the forking process to already
be root — got a dedicated privileged helper (`siphonophore-spawn`) rather than a bare `sudo -u`
grant, on the reasoning that a broad grant covering the whole ephemeral uid range with an arbitrary
command is "smaller than root but still surprisingly powerful." Interface frozen first, deliberately
before implementation, at `contracts/spawn_helper.md` — matching this project's own stated
discipline (§4: name every trust boundary; freeze the contract, don't build against a moving one).

Two rounds of review found real gaps in the frozen contract, both incorporated as amendments before
any code was written to defend the original wording:

- **Round one (capability audit):** the original draft let `uid`+`username` alone authorize a
  spawn — only a reusable account, not a specific execution — and let cgroup membership happen
  *after* the artifact could run, a real regression versus the sync-pipe handshake the existing
  `UidCgroupBackend` already uses. Fixed by binding spawn to `execution_id` and cgroup-leaf creation
  (`SH-23`), pinning final-runtime fd numbers (`SH-24`), and replacing a genuinely deadlocking
  pipe-based transport design with sealed `memfd`s (`SH-25` — a plain pipe blocks the writer once
  content exceeds the kernel buffer, and no reader exists until after the privileged helper exec's
  the runtime that would read it).
- **Round two (trust-boundary correction):** the round-one fix for `SH-23` itself overclaimed —
  "successfully creating the cgroup leaf is the proof of one-shot authorization" reads as a claim
  that leaf existence proves `Gate.submit()`-level authorization, but the broker holds real,
  delegated capability to create *any* leaf under the configured subtree, independent of any Gate
  decision. Resolved by narrowing the claim, not by adding a second verification mechanism: `SH-23`
  now states its actual guarantee precisely — execution-identity consistency and replay prevention,
  nothing more — and says explicitly that it cannot and does not attest that the originating
  request was Gate-authorized. Explicitly declined to close that residual gap by sharing the Gate's
  own secret with the helper, or building a separate authorization-capability subsystem, for two
  reasons: it would expand the helper's own trusted surface (against the whole point of this
  redesign), and it wouldn't actually help against the threat that matters — a broker whose own
  process is compromised already holds the Gate's secret in memory and can mint a genuinely valid
  `Decision` without going near `siphonophore-spawn` at all. That residual gap is the same one
  DESIGN.md §8 already names and leaves open: who attests the broker's own integrity. Also fixed a
  smaller, related overclaim in `SH-25`: seals must be applied and independently verified *before*
  the descriptor is exposed to the final runtime, and that descriptor must be opened read-only from
  the start — otherwise "sealed memfd" sounds stronger in the contract's prose than the actual fd
  rights prove.

**Status unchanged from before either amendment: PINNED, zero implementation.** The next real
session on this thread should read the contract fresh (`contracts/spawn_helper.md`, "Amendment
history" section at the end), confirm it still looks right, then implement — `siphonophore-spawn`
itself, the broker's client-side envelope writer, one test per `SH-NN` invariant.

## Architectural context arrived: siphonophore as a policy boundary under NeMo Fabric, not a standalone harness

A separate context document (`SIPHONOPHORE_NEMO_CONTEXT.md`) reframed the project's own scope:
build against NVIDIA NeMo Fabric rather than grow another general-purpose agent harness. Fabric
owns the generic runtime concerns — model/provider integration, multi-agent orchestration,
lifecycle, tools/MCP plumbing, telemetry — and NVIDIA's existing sandbox technologies should be used
where they satisfy the required isolation guarantee rather than duplicated inside siphonophore.
Siphonophore's own job narrows to the security decision between agent intent and execution:
authorize/deny, establish attributable execution identity, determine required isolation, select
execution substrate — with the substrate itself (uid+cgroup, NVIDIA sandbox, container, VM) chosen
by risk, not prescribed as one fixed topology.

This makes the spawn-helper work *more* coherent, not obsolete — `siphonophore-spawn` becomes one
substrate adapter (the Linux uid+cgroup one) beneath a policy boundary that's explicitly supposed to
support several, not the architecture's own center of gravity. It also sharpens the trust-boundary
conclusion reached the same day: authorization belongs at the Gate/policy layer, above every
substrate adapter including this one — a principle this framing states directly rather than leaves
implicit.

**Not yet done: reconciling DESIGN.md's own stated thesis and shape (§0, §6 in particular) against
this framing.** The document's opening line still describes siphonophore as "an SDK for mediated,
attributable agent harnesses" — true of the mechanism, but no longer the framing this context
document gives for why that mechanism exists or what sits above/below it in a real deployment. The
code built so far survives this reframing without changes; what needs updating is the conceptual
ownership boundary DESIGN.md draws around the project, not any invariant it currently enforces.

## `siphonophore-spawn`: implemented and validated for real, not just reasoned through

The third and last piece of the broker-root-privilege gap moved from "interface frozen, zero code"
to "built and validated for real on colima" in one pass. C, chosen deliberately over Python for
this one component specifically: the whole point of `siphonophore-spawn` is to be a minimal,
narrow, privileged trust boundary, and pulling the CPython interpreter's own startup machinery,
import system, and stdlib surface into a root-executed binary works against that in a way developer
convenience doesn't justify. Everything upstream and downstream of this one binary — policy,
authority reasoning, the broker itself, the bootstrap runtime the helper hands off to once
privilege is already dropped — stays Python, unaffected.

Implementation lives in `spawn_helper/` (`siphonophore-spawn.c`, `bootstrap.py`, `Makefile`,
`README.md`), dependency-free beyond libc: no JSON library (a small hand-rolled parser for the
one fixed, flat envelope shape `SH-09` defines), no regex library (character-class validation
matching `scripts/siphonophore-useradd`'s own convention exactly). Every `SH-NN` invariant in
`contracts/spawn_helper.md` maps to a labeled block in the C source and a named test in
`tests/test_spawn_helper_linux.py` — 16 tests, all passing for real on colima, including:

- The full happy path: real uid drop (verified via `getuid()`/`geteuid()` from inside the spawned
  artifact, not just assumed from the helper's own claim), real cgroup membership, payload passed
  through intact.
- `SH-25`'s read-only exposure claim, checked by having the artifact itself attempt `os.write()`
  and `os.ftruncate()` against its source/payload fds and confirming both fail `EBADF` — not just
  that seals were applied, but that the descriptor the artifact actually receives can't write at
  all.
- `SH-22`'s argv-avoidance claim, checked against the spawned process's own `/proc/self/cmdline`
  directly, the same discipline `lab/005` established for the nonce originally — a marker value
  planted in the payload was confirmed genuinely absent from argv, not assumed absent from design.
- `SH-23`'s replay prevention: a second spawn attempt against an already-consumed `execution_id` is
  refused (exit 23); a first, fresh attempt with the same identity succeeds.
- `SH-21`'s liveness invariant, exercised with a genuinely blocked client (a held-open pipe that
  sends a valid envelope header and then withholds the declared body, never closing) — confirmed
  the `SIGALRM` handler actually fires at the configured bound (not sooner, not never) and exits
  cleanly, the one code path a normal test can't reach any other way since it requires exercising a
  real signal handler under real I/O blocking, not simulating one.
- All ten `SH-12`..`SH-17` fail-closed conditions, each its own test asserting the exact exit code
  that invariant is supposed to produce, not just "something failed."

Also validated manually against a real, narrow sudoers grant (not automated in the pytest suite,
matching how the `useradd`/`userdel` grant was validated earlier — provisioning system-level
sudoers config isn't something to wire into CI casually): a genuinely unprivileged test user,
zero password prompt via `sudo -n`, and — the specific property `SH-08` depends on — an attempt to
invoke the helper with any extra argument at all was refused *by sudo itself*, before ever reaching
the binary, proving the sudoers `""` argument-free syntax actually behaves as documented on this
sudo version rather than assuming it from a template comment.

**A real bug found and fixed during this pass, not just during design review:** the first
implementation's cleanup-on-failure logic tried to `rmdir()` a cgroup leaf it had created while the
failing helper process was itself still a live member of that leaf — cgroup v2 refuses to remove a
non-empty `cgroup.procs`, so the `rmdir()` silently failed and the leaf leaked, discovered by
actually running the fail-closed test battery and inspecting `/sys/fs/cgroup/siphonophore-core`
afterward rather than trusting the exit codes alone. Fixed by moving the process back into the
parent cgroup (`CGROUP_ROOT`, which has no controllers enabled on its own `subtree_control`, so this
is legal) before attempting the leaf's removal — shared between the normal failure path and the
`SIGALRM` handler via one async-signal-safe cleanup routine (hand-rolled integer-to-string instead
of `snprintf`, which glibc does not guarantee safe inside a signal handler). Added as its own
regression test (`test_a_failure_after_joining_the_cgroup_still_removes_the_leaf`) rather than left
as something the fix notes describe but nothing checks going forward.

Also fixed the same day, smaller and unrelated to the helper itself: `provision_cgroup()` (the
existing, pre-helper Python path `UidCgroupBackend` still uses) never validated that `execution_id`
was safe to use as a path component before building `{cgroup_root}/exec-{execution_id}` — a latent
path-safety gap of the identical shape `siphonophore-spawn.c`'s own `execution_id` validation
exists to close. Given the two now enforce the identical construction, letting them silently
diverge on what counts as valid would be its own future bug; fixed with the same character-class
check on the Python side, covered by a new portable test
(`tests/test_provision_cgroup_execution_id.py`).

**Still open, unchanged by any of this:** `siphonophore-spawn` is a validated, standalone mechanism
now — it is not yet wired into an `ExecutionBackend` `Executor` actually dispatches to. That
integration (a new backend, or an unprivileged-mode branch of `UidCgroupBackend`) is separate,
later work. Also unchanged: what this helper does and does not prove about authorization — see
`contracts/spawn_helper.md`'s `SH-23` section and DESIGN.md §8 for why that's a deliberate scope
boundary, not an oversight.

## A maturity assessment found the central claim unproven; Order/Authority/Scope fixed it for real

A trace-the-actual-code assessment (not design docs, not comments) against DESIGN.md §7's own
central claim -- "a second intent shape, delegation, must be demonstrated reducing to the exact same
primitive a tool call does" -- found it had only ever been proven inside `lab/002`, a self-contained,
deliberately isolated script (DESIGN.md §0), never carried into `siphonophore_core`. Every existing
test using `kind="delegate"` (`test_harness_broker.py`, `test_harness_loop_linux.py`,
`test_execution_uid_cgroup_checkin_linux.py`, `test_execution_uid_cgroup_env_linux.py`) proved only
that the string dispatched identically to `"run_artifact"` -- repeated mediation, not delegated
authority provenance. `Executor.execute()` had zero branches on `intent.kind`; `Decision`/`Intent`
carried no lineage field at all.

**The first fix attempted was itself a category error, caught before any code was written for it.**
A draft plan bound `parent_intent_id`/`root_intent_id` directly onto `Decision`, and used
`execution_class` as the ceiling a delegator could not let a delegate exceed. Both were wrong for
the same underlying reason: an Intent is an attempted exercise of authority, not its source, so
lineage doesn't belong on `Decision`/`Intent` at all; and isolation strength (execution_class) and
delegated authority are different questions entirely -- a sub-agent legitimately needing *stronger*
isolation than its delegator for one task is not a scope violation, and the ceiling-based design
would have wrongly refused it.

**The corrected model: `siphonophore_core/authority.py`** -- `Order` (the ungrounded root: an
originating authorization and its issuer, not itself an Intent), `Authority` (a standing,
principal-scoped capability, derived from an Order or from a verified parent Authority, never from
an Intent), `Scope` (deliberately minimal: `allowed_kinds` + `remaining_delegation_depth`, no
per-payload constraints, no execution-class dimension at all). `Gate` (`mediation.py`) gained
`issue_order()`, `grant_root_authority()`, `delegate()` -- each independently re-verifying its own
input before minting, the identical discipline `Executor.execute()` already applies to every
Decision -- and `submit()` gained an optional `authority` parameter: omitted, byte-for-byte
unchanged from before this existed; given, three checks run before policy (re-verify the Authority;
`intent.principal_id == authority.principal_id`, closing a real authority-impersonation gap the
category-error draft never had a mechanism for at all; `intent.kind` within the exercised scope).
`Decision` gained two optional fields (`authority_id`, `order_id`), both HMAC-bound alongside the
existing five -- a pointer to what grounded a Decision, not a delegation ceiling of its own.

The precise guarantee this produces was worth stating carefully rather than overselling: a delegated
Authority's lineage fields attest that Gate, at minting time, independently verified its parent and
enforced the derivation rules -- not that the child object independently reconstructs the whole
ancestry chain in isolation. That stronger property needs independent per-link signatures (a
macaroon-style scheme); this system has one Gate mediating every hop with one secret, so by
induction the chain is sound as long as Gate's own re-verify-before-mint discipline held at every
step -- a real, meaningful, but different claim, documented as such in `Gate.delegate()`'s own
docstring and DESIGN.md §9.

**`"delegate"` removed from `ConsequencePolicy.DEFAULT_ALLOWED_KINDS` entirely** -- it was never
Executor-handled, so there was no real behavior to preserve, and keeping it as an inert placeholder
was itself the anti-pattern this fix exists to correct. Delegation is now `Gate.delegate()`, a grant
operation, never an Intent kind. Every test that used `kind="delegate"` incidentally (check-in,
env-leak-probe tests with no actual delegation content) was mechanically repointed to
`kind="run_artifact"`; `test_harness_broker.py`'s now-invalid positive claim was replaced with a
negative test confirming `"delegate"` is correctly refused as an ordinary kind, not silently
accepted; `siphonophore_harness/prompts.py`'s model-facing vocabulary and `broker.py`'s docstring
(which had claimed delegation "reduces to the same primitive... by construction, not a case Broker
adds for it" -- no longer true, Broker doesn't support authority-aware dispatch) were both corrected
rather than left stale.

**The real vertical slice** (`tests/test_harness_loop_linux.py`,
`test_a_delegates_constrained_authority_to_b_who_executes_via_uid_cgroup`) composes the whole
corrected path for real on colima: a real `Order`, principal A's root `Authority` derived from it, a
narrower `Authority` delegated to B, B's own `Intent` submitted against that Authority (checked
against B's actual delegated scope, not just "some delegation exists"), landing in the real,
`useradd`/cgroup-v2-backed `UidCgroupBackend` -- real uid drop, kernel-verified via
`/proc/<pid>/status`, confirmed independent of and unrelated to the authority mechanism entirely.
Deliberately bypasses `Broker`/`CognitiveLoop` for the authority-exercising step (`Broker.dispatch()`
has no `authority` parameter) rather than papering over that gap -- named explicitly in DESIGN.md's
"Explicitly open" section as real, deferred integration work, not solved silently.

`tests/test_authority.py` (portable, 15 tests) covers the mechanism's own properties in isolation:
fabricated/missing lineage, a real chain splicing attack (two independently-valid `Order`->`Authority`
chains, one's lineage fields swapped to claim the other's, real tokens unchanged -- fails
verification, same mechanism `lab/002`'s kind-relabel case proved for `kind`), scope expansion at
both delegation-mint time and exercise time, authority impersonation via principal mismatch,
delegation-depth exhaustion, and -- composed with the pre-existing, unrelated mechanisms rather than
duplicating them -- artifact substitution and execution-class downgrade demonstrated holding on an
authority-grounded Decision, proving those two guarantees are genuinely orthogonal to the new
authority layer, not something it happens to also cover.

Full suite: 123 passed portable (up from 105 before this pass), 155 passed on colima including every
`linux_root_only` test, zero regressions. Host confirmed clean after the real-root run.

**Still genuinely open, named in DESIGN.md rather than left implicit:** `Broker`/`CognitiveLoop`
don't expose authority-aware dispatch; no real second `CognitiveLoop`/multi-agent orchestration
exists; Scope's per-payload/per-resource dimension remains deliberately unbuilt; `principal_id` is
still a bare string with no org/firm layer above it, despite §6's module layout having anticipated a
real `Principal` class since early in this project.

## siphonophore-spawn wired into a real ExecutionBackend -- the broker-root-privilege gap fully closed

The third piece was implemented and validated on its own (see the earlier entry), but stood
outside the actual dispatch path -- `Executor` had no way to reach it. Closed by
`siphonophore_core/execution_spawn_helper.py`'s `SpawnHelperBackend`, a real `ExecutionBackend` for
the `uid_cgroup` class implemented entirely as a client of `siphonophore-spawn` rather than
`preexec_fn`. Traced `UidCgroupBackend`, `Executor.execute()`, the spawn-helper contract, and every
existing Linux test shape before writing anything: `provision_ephemeral_user()`/
`release_ephemeral_user()` were already unprivileged-broker-compatible and reused unchanged;
`provision_cgroup()`/`add_pid_to_cgroup()`/`release_cgroup()` were correctly identified as NOT
reusable, since cgroup creation for this path has to stay entirely inside the helper (`SH-23`) --
the broker independently creating leaves would reopen exactly the gap the helper's own trust-
boundary section already names as a limit on what it can prove; `Executor.execute()` needed zero
changes at all, since its existing Decision/artifact-digest verification already runs before any
registered backend, regardless of which one.

**A real design question was surfaced and resolved before writing code, not discovered afterward:**
who removes a cgroup leaf once its execution finishes? Delegating `CGROUP_ROOT` to the broker for
cleanup was considered and rejected -- it would let the broker independently create leaves too,
reopening the gap `SH-23`'s own trust-boundary discussion already names. A narrower alternative (a
separate, small privileged wrapper script that only removes a validated, empty leaf, never touching
`siphonophore-spawn.c` itself) was also considered and rejected on closer inspection: even that
would let a broker delete a finished execution's leaf and then replay the identical `execution_id`
through the helper again, defeating `SH-23`'s stronger, previously-unstated property (one real
spawn, ever, per `execution_id`) rather than just its concurrent-reuse guarantee. Resolved by
choosing not to build either: `SpawnHelperBackend` leaves finished executions' cgroup leaves in
place, documented explicitly in the backend's own module docstring, `DESIGN.md`, and here -- the
real cost is low (an unremoved cgroup v2 leaf is a near-zero-weight kernfs entry, not a resource
problem in practice), and choosing not to build a broker-triggerable removal path is not itself a
weakening of anything, unlike either alternative would have been.

Validated on colima with something stronger than every prior `linux_root_only` test: rather than
running pytest itself as root and exercising root-requiring code directly, the new test
(`tests/test_execution_spawn_helper_linux.py`) provisions a real, unprivileged system user and a
real, narrow sudoers grant (covering exactly the `useradd`/`userdel` wrapper scripts and the
argument-free `siphonophore-spawn` invocation, not `ALL`), then runs the actual `Gate`/`Executor`/
`SpawnHelperBackend` dispatch code inside a genuinely separate subprocess running as that
unprivileged user (`sudo -u`) -- confirming the broker's own Python process never held euid 0, not
assuming it from the mechanism working when invoked from an already-root test process. The artifact
landed under a real, different ephemeral uid inside a real, kernel-verified cgroup, and a second
test confirmed artifact-substitution refusal still fires before `siphonophore-spawn` is ever
invoked, exactly as it does for every other backend, by construction. Full suite: 157 passed on
colima (was 155), 123 passed portable, zero regressions, host confirmed clean (modulo the disclosed
cgroup-leaf limitation itself, which recurred exactly once per test run as expected and was cleaned
up manually, consistent with it being a known, low-cost, undealt-with limitation rather than a bug).

## Broker.dispatch() made authority-aware -- delegation reachable through the public interface

The real delegation vertical slice (previous entry) worked, but had to bypass `Broker` entirely:
`Gate.submit(intent, authority=...)` and `Executor.execute()` stitched together by hand, since
`Broker.dispatch()` only ever called the authority-less `Gate.submit(intent)`. A caller
demonstrating delegation had to know `Gate`/`Executor` existed at all -- exactly the kind of
escape hatch `Broker`'s own docstring already argued against for every other case.

Fixed with the smallest possible change: `dispatch(self, intent, authority=None)`. Omitted, behavior
is byte-for-byte unchanged from before this parameter existed. Given, it's threaded straight to
`Gate.submit(intent, authority=authority)`, which already does its own independent re-verification
of that Authority -- `Broker` adds no logic of its own, it only removes the need to bypass it.
Granting authority itself (`issue_order`/`grant_root_authority`/`delegate`) stays outside `Broker`
deliberately -- those aren't Intents, so routing them through `dispatch()` would have been the same
category error `"delegate"`-as-an-Intent-kind already was.

Rewrote the real end-to-end slice (`tests/test_harness_loop_linux.py`) so B's delegated effect goes
through `broker.dispatch(sub_intent, authority=authority_b)` only -- Order/Authority setup (the
grant side, not an effect) stays as direct `Gate` calls, matching where the design actually puts
that boundary. Added a negative case through the same interface: B attempting a kind outside its
delegated scope is refused via `GateViolation` propagating out of `dispatch()` itself, not a
separately-checked `Decision`. Added a portable counterpart (`test_harness_broker.py`, two new
tests, `SameProcessBackend`, no root needed) proving the same positive/negative shape without
needing colima, since the authority mechanism itself is pure Gate logic.

125 passed portable (was 123), 159 passed on colima (was 157), zero regressions, host confirmed
clean (the disclosed spawn-helper cgroup-leaf leftover recurred exactly once, as expected, and was
cleaned up manually).

This gives Siphonophore a claim worth stating precisely, since every piece of it now has a specific
test backing it rather than being asserted: a principal can delegate bounded authority to another
logical actor: `Authority` re-verified, scope attenuation enforced, root traced (`test_authority.py`,
`Gate.delegate()`); that actor exercises it through the harness's ordinary public dispatch path:
`broker.dispatch(intent, authority=...)`, no bypass needed (`test_harness_broker.py`,
`test_harness_loop_linux.py`); the authorization is independently re-verified before execution:
`Gate.submit()`'s three checks, `Executor.execute()`'s Decision/artifact re-verification, both
unconditional; and the resulting effect executes under a real OS identity through a privilege
boundary the broker itself does not possess: `SpawnHelperBackend`, confirmed via a genuinely
unprivileged broker subprocess (`test_execution_spawn_helper_linux.py`). Each clause has its own
named test, not just an architecture diagram implying the composition.

## Check-in and Belnap reconciliation composed with delegation, without touching spawn-helper's own contract

The last piece named in the user's own sequencing: compose `CheckedInUidCgroupBackend`'s real
check-in protocol and Belnap reconciliation into the same delegated-authority path
`SpawnHelperBackend` and `Broker.dispatch(intent, authority=...)` already proved for real.

**The first, necessary step was an architectural check, not an implementation guess:**
`CheckedInUidCgroupBackend` cannot be composed with an unprivileged broker as it stands --
`require_real_root_linux()` is called directly in its constructor, and its `run()` uses
`preexec_fn=_drop_privileges` (a bare `os.setuid()` in a forked copy of the broker's own process),
the identical constraint `siphonophore-spawn` exists to remove for the plain `uid_cgroup` class.
Reusing it directly would have silently reintroduced root into the broker -- exactly what the user
asked to be stopped and explained rather than worked around.

**What made composing the two possible without modifying either was a fact hiding in the already-
pinned contract:** `SH-09`/`SH-24` already define an optional nonce field and a fixed fd (5) for it
-- built when the spawn-helper contract was originally frozen, deliberately anticipating future use,
never exercised until now. `bootstrap.py`'s own docstring already said as much: "reading it is the
artifact's own job... if and when it performs a check-in." Composing check-in required zero changes
to `siphonophore-spawn.c`, `contracts/spawn_helper.md`, or `CheckedInUidCgroupBackend` -- confirmed
`siphonophore_core` is importable from the artifact's own sanitized environment first (it is,
editable-installed system-wide, not `PYTHONPATH`-dependent), then built
`execution_spawn_helper_checkin.py`'s `CheckedInSpawnHelperBackend`: generates a real nonce, sends
it through the existing nonce channel, wraps `intent.artifact_code` (mirroring, not duplicating,
`_CHECKIN_CHILD_WRAPPER`'s shape, adapted to `bootstrap.py`'s calling convention -- `payload`/
`NONCE_FD` arrive as globals, not argv) so the artifact performs the identical
`identity.perform_checkin()` call the existing backend already requires. Registered under the same
`uid_cgroup_checkin` execution class `CheckedInUidCgroupBackend` uses -- a deployment choice of
implementation, not a new Gate/Executor/Decision concept, mirroring exactly how `SpawnHelperBackend`
already relates to `UidCgroupBackend` under `uid_cgroup`.

**A real deadlock/reap-ordering bug was found and fixed during validation, not caught by review:**
writing the envelope+source+payload+nonce stream to the helper's stdin on a background thread (to
avoid blocking while concurrently waiting on the check-in registry) meant that thread closed
`proc.stdin` itself -- but `subprocess.communicate()`, called afterward from the main thread to
collect final output, still tried to flush/close `self.stdin` internally and raised `ValueError:
flush of closed file` *before* it finished waiting for and reaping the child. The exception
propagated past the intended wait, so `release_ephemeral_user()` ran while the artifact process was
still alive, and a real `userdel: user ... is currently used by process <pid>` failure surfaced
immediately on the first real colima run -- caught by running the test for real, not by inspection.
Fixed by clearing `proc.stdin = None` after the writer thread finishes, telling `communicate()` to
skip stdin handling entirely rather than touch an fd it no longer owns.

Validated on colima with one real, single composed execution
(`tests/test_harness_loop_linux.py::test_delegated_effect_produces_independently_verified_and_reconciled_evidence`):
a real `Order`, principal A's root `Authority`, a narrower `Authority` delegated to B, B's own
`Intent` submitted through `broker.dispatch(intent, authority=authority_b)`, landing in
`CheckedInSpawnHelperBackend` -- real uid+cgroup identity via `siphonophore-spawn`, a real,
kernel-verified check-in (`SO_PEERCRED`) tied to that specific `execution_id`, and a real
reconciliation result attached to the same `Effect`. The negative case
(`test_fabricated_self_report_is_not_reconciled_as_confirmation`) proves the distinction that
matters: B's check-in genuinely, correctly verifies (its identity is real), but its self-report
lies about one path's content and omits a real undisclosed write -- neither reconciles as
`corroborated`; a false or incomplete claim is refused as confirmation even when the identity behind
it is completely genuine, which is exactly the boundary DESIGN.md section 3 exists to keep sharp.

161 passed on colima (was 159), 125 passed portable, zero regressions, host confirmed clean.

This closes the sequence the user laid out explicitly: unprivileged-broker `uid_cgroup` execution
(#1), authority-aware `Broker.dispatch()` (#2), and now check-in/Belnap composition (#3) -- each
landed as the smallest change that reused, rather than duplicated or modified, what the previous
step had already proven. Siphonophore's claim is now, in full: a principal delegates bounded
authority to another logical actor; that actor exercises it through the harness's ordinary public
dispatch path; the authorization is independently re-verified before execution; the effect executes
under a real OS identity through a privilege boundary the broker itself does not possess; and that
execution is independently, kernel-verified and reconciled against an untrusted self-report, with
a false claim refused as confirmation even when the identity behind it is real.
