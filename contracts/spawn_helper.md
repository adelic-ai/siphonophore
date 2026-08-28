# Contract: `siphonophore-spawn` — the privileged helper that closes the broker-root-privilege gap

**Status:** PINNED and IMPLEMENTED, 2026-08-27. Amended twice on 2026-08-26 after two rounds of
capability-audit review (see "Amendment history" at the end), then implemented in C
(`spawn_helper/siphonophore-spawn.c`) and validated for real on colima — every `SH-NN` invariant
below has a corresponding automated test in `tests/test_spawn_helper_linux.py`, run for real as
root against a genuinely unprivileged `sudo`-mediated invocation, not just reasoned through. See
`HISTORY.md`'s "siphonophore-spawn: implemented and validated for real" entry for what was actually
exercised, including one real bug the implementation pass found and fixed (a cgroup-cleanup
ordering issue not visible from reading the contract alone).

## Why this exists

`useradd`/`userdel` and cgroup management no longer require the broker to be root (see
`scripts/README.md`, `HISTORY.md`'s "Privilege separation for useradd/userdel" entry). The one
remaining piece: spawning the artifact process under its target ephemeral uid. `preexec_fn`
calling `os.setuid()` requires the *forking* process to already be root — a hard Linux constraint.
`siphonophore-spawn` is the minimal, narrowly privileged helper that performs exactly that step on
the broker's behalf, so the broker's own Python interpreter never needs to hold uid 0.

## The one property this whole contract exists to protect

**The broker supplies only bounded data and a target identity. The helper alone owns execution
semantics** — which interpreter, what flags, what environment, what groups, what cwd, and (as of
this amendment) what cgroup. The moment the broker can specify any of those, `siphonophore-spawn`
stops being a narrowly privileged mechanism and becomes a generic root-execution proxy with a
nicer protocol wrapped around it. Every invariant below is in service of this one property; none
of them is optional.

A second, related property, made explicit by this amendment: **the broker's authority is to spawn
*this specific, not-yet-consumed execution slot* — not "any currently valid siphonophore ephemeral
account."** `uid`+`username` alone identify an account that could, in principle, already be in use
by a different, unrelated execution; `execution_id` (§`SH-23`) is what prevents that reuse/collision
from a single spawn attempt. **What `execution_id` does *not* do — see `SH-23`'s trust-boundary
note — is prove that this spawn was ever authorized by `Gate.submit()` in the first place; that
verification happens one layer up, inside the broker, before the helper is ever invoked.**

## Actors and flow

```
unprivileged broker
      │  creates the source/payload memfds and the nonce pipe itself (SH-11, SH-25)
      │  writes: control envelope (incl. execution_id), then source bytes, payload bytes,
      │  nonce bytes if present -- ONE multiplexed stream over the helper's stdin (SH-10)
      ▼
sudo -n /usr/local/libexec/siphonophore-spawn      <- exact fixed command, no broker-controlled argv
      │
      ├─ SH-01 read and validate the control envelope (version, declared lengths, uid, username,
      │        execution_id)
      ├─ SH-02 cross-validate identity: uid in range, username matches convention,
      │        getpwnam(username).pw_uid == uid
      ├─ SH-03 read exactly code_length source bytes, exactly payload_length payload bytes,
      │        exactly nonce_length nonce bytes if present -- capped independently of what the
      │        envelope claims
      ├─ SH-23 derive the target cgroup path from fixed configuration + execution_id (never a
      │        broker-supplied path); refuse if that cgroup leaf already exists (one-shot
      │        consumption); create it; add this process to it -- BEFORE privilege drop
      ├─ SH-04 construct the sanitized child environment itself (does not trust anything the
      │        broker sent)
      ├─ SH-25 seal source/payload into memfds at fixed fd numbers; nonce stays on its inherited
      │        pipe (SH-24)
      ├─ SH-06 setgroups([]) / setgid(gid) / setuid(uid)
      ├─ SH-07 close every helper/control fd that isn't one of the fixed set handed to the final
      │        runtime
      ▼
fixed bootstrap runtime, running AS the target uid, already a cgroup member, privilege dropped
      │
      └─ reads source / payload / nonce each from its own fixed, pre-agreed fd (SH-24) -- none
         of it ever touched argv, anywhere in the chain
```

## Invocation shape (`SH-08`)

The sudoers grant is an **exact, argument-free command** — `siphonophore-spawn` takes nothing on
argv. This is a security property, not an implementation convenience: an exact-match sudo command
has no string-vs-execution gap for a validator to be fooled by. (Directly the same bug shape
"Trusted Enough to Run", Black Hat USA 2026, documents in Claude Code's Round 1: 23 shell-security
validators reading a command as text while `git` itself parsed and executed the identical string
differently. An exact-match invocation with everything else carried as opaque, length-framed data
has no such string to parse two different ways.)

## Control envelope (`SH-09`)

A small, fixed-shape header — protocol/version and identity/length/execution metadata only.
**Never**: an executable path, argv, environment variables, cwd, groups, interpreter flags, a
shell selector, or a cgroup path — see `SH-23`.

```
{"version": 1, "uid": 60017, "username": "sipho-core-a1b2c3d4", "execution_id": "a1b2c3d4-...",
 "code_length": 1842, "payload_length": 517, "nonce_length": 0}
```

Followed, on the same stream, by exactly `code_length` raw bytes, then exactly `payload_length`
raw bytes, then (only if `nonce_length > 0`) exactly `nonce_length` raw bytes. Header/length framing,
not base64-in-JSON — gives a hard maximum size and makes truncated/oversized/malformed input
straightforward to detect and reject (`SH-14`).

## Transport (`SH-10`)

**Broker → helper (crosses the `sudo` boundary): one multiplexed stream over the helper's stdin.**
`sudo` closes file descriptors above stderr by default; relying on multiple `pass_fds` surviving a
`sudo` invocation requires `closefrom_override` plus `-C`, configuration that has not been
verified and should not be assumed. stdin is preserved by default and needs no such
configuration — so everything broker→helper (envelope, source, payload, nonce) travels as one
ordered, length-prefixed stream over stdin, not separate fds.

**Helper → final runtime (no `sudo` involved at this hop): fixed fd numbers, memfd for bulk data,
pipe for the nonce** — see `SH-24`/`SH-25`. No `sudo` boundary to lose fds across at this inner
hop, but a plain pipe has its own real hazard here (`SH-25`'s rationale), which is why source and
payload specifically don't stay pipes past this point.

## Pipe ownership (`SH-11`)

**The broker creates every pipe that carries data to the helper, before invoking `sudo`, and
passes only the resulting stdin stream — the helper never opens a broker-named filesystem path to
obtain source, payload, or nonce material.** A "source path" or "payload path" field in the
envelope would be a confused-deputy surface: the unprivileged broker naming an arbitrary path, the
privileged helper reading whatever that path actually points to. There is no such field, and there
must never be one. The same principle governs `SH-23`: the *cgroup* path is likewise never
broker-supplied, for the identical reason.

## Fail-closed conditions (`SH-12` through `SH-19`)

Each of these is a distinct, independently testable refusal — not folded into one generic error:

- `SH-12` unknown/unsupported `version`
- `SH-13` malformed envelope framing (not valid JSON, missing required field, wrong type)
- `SH-14` declared length exceeds the helper's own hardcoded maximum (independent of what's
  claimed — the helper never allocates or blocks on an unbounded read just because a length field
  says to)
- `SH-15` short read — fewer bytes arrive than the declared length before the stream closes
- `SH-16` extra/trailing bytes where the protocol expects EOF
- `SH-17` uid outside the configured ephemeral range, username not matching the naming convention,
  `getpwnam(username).pw_uid != uid`, or (as of this amendment) `execution_id` not matching the
  expected format
- `SH-18` any fd-related failure — pipe/memfd creation, `pass_fds`/`dup2` setup, or an fd already
  in an unexpected state
- `SH-19` privilege-drop failure (`setgroups`/`setgid`/`setuid` raising) — the helper must not
  proceed to construct or exec the final runtime if any of these three calls fails, and must not
  leave the failure ambiguous (partial drop is not an acceptable state to exec from)

## Cgroup membership and one-shot execution binding (`SH-23`)

**Added by the first capability-audit amendment; narrowed to a true claim by the second (this
one) after a further review correctly identified that the original wording overclaimed what this
mechanism actually establishes.** Two things, resolved together because they're the same
mechanism:

1. **The final process must already be a member of its target cgroup before any
   artifact-controlled instruction executes.** Without this, there is a real window — the helper
   has dropped to the target uid and exec'd, but the broker (in the old, pre-helper design) hasn't
   yet written the pid into `cgroup.procs` — during which the artifact runs as the right uid but
   outside its claimed cgroup. The *existing* `UidCgroupBackend`/`_CHILD_WRAPPER` already solves
   this today via a sync-pipe handshake (the child blocks on a read before running artifact code;
   the parent adds it to the cgroup first, then signals go-ahead) — this contract's first draft
   simply failed to carry that already-proven mechanism forward. Fixed here, not deferred.
2. **`uid`+`username` alone identify a reusable *account*, not a specific spawn attempt.** A buggy
   or compromised broker could invoke the helper twice with the same valid uid/username pair,
   running unrelated code under an identity another execution is already using.

Both close via the same construction: the helper derives the target cgroup path from **fixed
helper-side configuration + the envelope's `execution_id`** — e.g. `{CGROUP_ROOT}/exec-{execution_id}`,
matching `provision_cgroup()`'s own existing naming convention — never from anything the broker
supplies as a path. Creating that leaf is `mkdir(..., exist_ok=False)`: if it already exists, the
helper refuses (`SH-17`) — that `execution_id` has already been used in an earlier spawn attempt,
so this request cannot be a first use of it. The process is added to that cgroup immediately after
creation, still as root, before `SH-04`/`SH-25`/`SH-06` proceed.

**What this precisely guarantees, stated narrowly:** for a given `execution_id`, the helper permits
at most one successful spawn into the correspondingly derived cgroup. The helper verifies that the
expected cgroup leaf does not already exist, binds the spawn to that leaf atomically with creating
it, rejects reuse/collision, and ensures cgroup membership before any artifact-controlled code
executes. This provides **execution-identity consistency and replay prevention.**

**What this does *not* guarantee, and cannot be made to: that the originating broker action was
ever authorized by `Gate.submit()`.** The broker holds real, delegated capability to create *any*
leaf under the configured cgroup subtree — that capability is exactly what cgroup delegation grants
it (`chown` the subtree once, no further code or root needed; see `scripts/README.md`). A leaf
existing, or an `execution_id` never having been used before, therefore proves only "the broker
asked for this specific, previously-unused name" — it says nothing about whether a `Decision` was
ever minted for it. Closing that gap for real would require the helper to independently verify
Gate-issued authorization, which it structurally cannot do without either (a) sharing the Gate's own
HMAC secret with the helper — expanding exactly the privileged surface this whole redesign exists to
narrow, since a second process would then hold key material capable of validating (and, depending on
shape, forging) authorization — or (b) a wholly separate authorization-capability subsystem with its
own secret and its own registration step, which is real, buildable future work, not something to
fold silently into a cgroup-naming convention. Either way, it is out of scope for `siphonophore-spawn`
specifically: **authorization belongs above the execution substrate**, at the Gate/policy boundary,
not inside the mechanism that merely carries out an already-authorized spawn.

This also does not newly weaken anything: a broker capable of fabricating a plausible `execution_id`
is, by definition, a broker whose own process has been compromised — and a compromised broker already
holds the Gate's own secret in memory, since they are the same process, meaning it could mint a
genuinely valid `Decision` for anything it wants without ever needing to go near `SH-23`'s
replay-prevention check at all. Gate-level cryptographic authorization defends against forgery *from
outside the process holding the key*, not against that process's own compromise. Whether the broker
itself is running the code it's supposed to be running is a question this contract cannot answer —
it is the same question DESIGN.md §8 already names and leaves explicitly, permanently open:
*"who attests the broker's own integrity."* `siphonophore-spawn` narrows what a compromised or buggy
broker can do (bounded, non-root, environment-sanitized, uniquely-scoped execution, never privilege
escalation) — it does not, and structurally cannot, establish that the broker itself should be
trusted in the first place.

## Fixed final-runtime fd numbers (`SH-24`)

**Added by this amendment.** The bootstrap runtime must be able to find source/payload/nonce
without the broker describing execution topology — that would be the same class of leak `SH-08`'s
argv-free invocation exists to prevent, just moved one layer down. Pinned:

```
fd 3 = source (sealed memfd, see SH-25)
fd 4 = payload (sealed memfd, see SH-25)
fd 5 = nonce (pipe, only when present)
```

The helper `dup2()`s each into place before `SH-06`'s privilege drop and `SH-07`'s fd cleanup.
Never communicated via an environment variable, a broker-supplied field, or runtime fd discovery —
the runtime's own bootstrap code hardcodes these three numbers, full stop.

## Source/payload transport: sealed memfds, not fresh pipes (`SH-25`, corrects the original `SH-05`)

**The original draft of this contract (`SH-05`) proposed the helper reading source/payload fully
into memory, then creating fresh pipes and writing that buffered content into them for the final
runtime to read. That has a real deadlock, not just an inefficiency:** a pipe's kernel buffer is
bounded (commonly 64 KiB on Linux); writing content larger than that buffer, before any reader
exists to drain it, blocks the writer — and the intended reader (the final runtime) doesn't exist
yet, because the helper hasn't exec'd it. The helper would hang inside its own privileged process,
holding root, on ordinary large artifact source. A hard max size does not save this unless it
happens to sit below the system pipe capacity, which is an accidental, fragile dependency, not a
designed one.

**Fixed via `memfd_create()`, sealed:** for source and for payload, the helper creates an anonymous
memfd, writes the exact declared-length bytes into it (already bounded by `SH-14`, no streaming
concern — an in-memory file has no fixed kernel buffer to overflow), seals it
(`F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW`, or equivalent) so nothing — helper included — can
modify it after this point, rewinds it to offset 0, and `dup2()`s it to the fixed fd number
(`SH-24`). No concurrent writer is ever needed, because by the time the final runtime is exec'd,
the content is already fully and immutably present — the runtime just reads from a fixed position.
Sealing is a second real property beyond solving the blocking problem: the final runtime is
guaranteed the bytes it reads are exactly what the helper validated, not something that could
still change underneath it — but only if the ordering below is followed exactly; sealing after
exposure, or exposing a writable descriptor, would make "sealed memfd" sound stronger in this
contract than the actual fd rights prove.

**Normative order, no step skipped or reordered:**

```
write bounded memfd (SH-14-capped)
  → apply required seals (F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW)
  → verify the seals actually took (query fd seal state, don't assume the call succeeded silently)
  → rewind to offset 0
  → expose the fixed fd number (SH-24) to the final runtime as read-only —
    the descriptor the artifact receives must not carry O_RDWR or O_WRONLY
  → drop privilege (SH-06)
  → exec
```

Sealing must complete, and be independently verified as having taken effect, **before** the fd is
`dup2()`'d into a descriptor the final runtime (and therefore the artifact) can see — a seal applied
after exposure, or an unverified seal call, leaves a window where the descriptor looks sealed in the
contract's prose but isn't actually immutable in practice. The artifact-facing descriptor is opened
read-only from the start, not opened read-write and merely sealed — sealing prevents shrink/grow/
further writes, but a descriptor that still carries `O_RDWR` is a distinct, additional capability
this contract does not intend to grant the artifact.

**Nonce stays a pipe, unchanged.** Its existing one-shot blocking-read semantics
(`read_nonce_from_fd()`) are already validated and small (a fixed-length hex string, nowhere near
pipe-buffer size) — no reason to touch a mechanism that isn't broken.

## Privileged helper startup hardening (`SH-26`)

**Added by this amendment.** Everything above constrains what the helper does with broker-supplied
*data*. None of it protects the helper's own *startup* from a broker-influenced environment — a
perfectly narrow protocol is worthless if `sudo` hands the helper a tampered environment or import
path before any of `SH-01` onward even runs. Required, regardless of whether the helper is
eventually implemented in C or Python (this contract does not decide that — see below):

- the helper binary/script, and anything it imports or loads, is root-owned and not writable by
  the broker's own user;
- the sudoers grant carries no `SETENV` authority — the broker cannot inject environment variables
  into the helper's own process via `sudo -E` or explicit `VAR=value` prefixes;
- `sudo`'s own environment sanitization (`Defaults env_reset`, the common default) is relied on
  explicitly, not assumed — verified present in the deployed sudoers configuration, not just
  hoped for;
- the helper runs from a fixed, absolute, root-owned path — never resolved via a broker-influenced
  `PATH` or relative location;
- if implemented in Python: no broker-controlled `PYTHONPATH`/import search path, and no loading
  of code or configuration relative to a broker-controlled current working directory.

## Ordering invariant (`SH-20`)

**Privilege is dropped, and every helper/control fd not explicitly handed to the final runtime is
closed, before any artifact-controlled instruction executes — and, as of this amendment, cgroup
membership (`SH-23`) is established before that same point too.** The full required ordering:
validate identity + `execution_id` (`SH-01`/`SH-02`) → establish cgroup membership (`SH-23`) →
construct environment (`SH-04`) → establish source/payload/nonce fds, seal-then-verify before
exposure (`SH-24`/`SH-25`) → drop supplementary groups, gid, uid (`SH-06`) → close everything else
(`SH-07`) → exec the fixed bootstrap. No source byte the broker supplied is interpreted as code, and
no payload byte is exec'd or evaluated, until all of the above has completed successfully. This
ordering is part of the guarantee this contract makes, not incidental plumbing.

## Liveness invariant (`SH-21`)

A killed helper, a malformed or truncated envelope, or a client that stops sending mid-stream must
not leave the helper blocked indefinitely, nor leave privileged state (an fd, a partially-read
buffer, a partially-created cgroup, anything) outstanding. Every read is bounded by both a byte cap
(`SH-14`) and a wall-clock timeout; every exit path — success, any `SH-12`–`SH-19`/`SH-23` refusal,
or an unexpected exception — releases whatever the helper itself allocated before that point,
including removing a cgroup leaf it created but never handed off to a running process.

## Argv invariant (`SH-22`)

Source, payload, and nonce material must never appear in the argv of any process in this chain —
not the `sudo` invocation, not the helper, not the final runtime. Verifiable directly against
`/proc/<pid>/cmdline` for each process in the chain during a real test run, the same way `lab/005`
originally confirmed the nonce's own argv-avoidance for real rather than assuming it.

## What this contract does not decide

- The exact language/runtime the helper itself is implemented in (a minimal C setuid binary vs. a
  small Python program invoked via the sudo grant) — a real choice with real tradeoffs (C has a
  much smaller trusted-computing-base surface but a much less familiar toolchain for this project;
  Python is consistent with everything else here but drags in the interpreter's own startup cost
  and attack surface into the privileged path, which `SH-26` constrains but does not eliminate).
  Decide when implementation actually starts, not here.
- The fixed bootstrap runtime's own exact shape (a small purpose-built reader vs. some other fixed
  entry point) — constrained by `SH-24`'s fd layout, not yet specified further.
- Resource limits (`rlimit`s) beyond the read-size caps already named in `SH-14` — worth deciding
  explicitly, not defaulting silently, but out of scope for freezing the wire contract itself.

## Amendment history

- **2026-08-26 (initial):** `SH-01` through `SH-22` drafted.
- **2026-08-26 (same day, capability-audit amendment):** a review specifically framed as a
  capability audit (not a general design pass) found two substantive gaps and corrected one
  drafting error before any implementation existed to defend it:
  - Added `SH-23` (cgroup membership before code executes, unified with one-shot
    `execution_id`-bound spawn authorization — the original contract let `uid`+`username` alone
    authorize a spawn, which only identifies a reusable account, not a specific execution).
  - Added `SH-24` (fixed final-runtime fd numbers — closes the same class of leak `SH-08` already
    prevents at the invocation layer, applied one layer down).
  - Corrected `SH-05` (renumbered/rewritten as `SH-25`) — the original "read fully, then write into
    fresh pipes" design has a real deadlock on any source/payload larger than the kernel pipe
    buffer; replaced with sealed `memfd`s, which also adds post-validation immutability as a
    property neither the original design nor a plain pipe provided.
  - Added `SH-26` (privileged helper's own startup hardening — a broker-influenced environment or
    import path reaching the helper itself was unaddressed by every invariant governing
    broker-supplied *data*).
  - Confirmed, not changed: broker-supplied arbitrary artifact source is intentional (that's the
    mechanism's whole purpose, constrained by *how* it can run, not *whether*); the
    multiplexed-stdin-over-`sudo` transport design from the initial draft holds.
- **2026-08-26 (same day, second amendment — trust-boundary correction):** a further review
  correctly identified that `SH-23`'s original wording overclaimed what cgroup-leaf creation
  actually proves — "successfully creating the leaf IS the proof of one-shot authorization" reads
  as a claim that the leaf's existence establishes `Gate.submit()`-level authorization, which it
  cannot, since the broker holds real delegated capability to create any leaf under the configured
  subtree independent of any Gate decision. Resolved by narrowing, not by adding a new verification
  mechanism to the helper:
  - `SH-23` rewritten to state its guarantee precisely as **execution-identity consistency and
    replay prevention** (at most one successful spawn per `execution_id`), and to state explicitly,
    in its own trust-boundary paragraph, that it does *not* attest that the originating broker
    action was Gate-authorized — that verification already happens one layer up, inside the broker,
    before `Gate.verify()` even lets the broker's own `Executor.execute()` proceed to invoke this
    helper at all.
  - Explicitly declined to add a second, helper-side authorization-verification mechanism (e.g.
    sharing the Gate's HMAC secret with the helper, or a separate authorization-capability
    subsystem) — considered and rejected for this contract specifically, both because it would
    expand the helper's own trusted surface (the property `SH-26` and this contract's central
    property exist to keep narrow) and because it cannot close the underlying gap regardless: a
    broker whose own process is compromised already holds the Gate's secret and can mint a
    genuinely valid `Decision`, making a second, helper-level check redundant against the actual
    threat it would need to defend against. **Authorization belongs above the execution
    substrate** — named here as a design principle, not just a one-off scoping call.
  - The residual gap (a compromised or sufficiently buggy broker minting an `execution_id` with no
    real `Decision` behind it) is tied explicitly to DESIGN.md §8's already-open, unresolved
    question — "who attests the broker's own integrity" — rather than left as an implicit,
    undocumented assumption of `SH-23`.
  - `SH-25` amended to make the seal-before-exposure ordering normative and independently verified
    (query seal state after applying seals, don't assume success), and to require the
    artifact-facing descriptor be opened read-only from the start rather than read-write-then-sealed
    — a smaller, separately-raised point in the same review, folded in here since both concern what
    a descriptor actually, verifiably proves versus what the prose claims it proves.
  - `SH-20`'s ordering invariant updated to reference the seal-then-verify step explicitly.

## Status

Implemented and integrated: `siphonophore-spawn` is wired into `SpawnHelperBackend`
(`siphonophore_core/execution_spawn_helper.py`) and `CheckedInSpawnHelperBackend`
(`siphonophore_core/execution_spawn_helper_checkin.py`), dispatched through the normal `Executor`
path for the `uid_cgroup` and `uid_cgroup_checkin` execution classes respectively -- not invoked
only manually or from tests. `tests/test_execution_spawn_helper_linux.py` runs the actual dispatch
code under a genuinely unprivileged broker subprocess.
