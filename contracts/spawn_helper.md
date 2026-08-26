# Contract: `siphonophore-spawn` — the privileged helper that closes the broker-root-privilege gap

**Status:** PINNED, 2026-08-26. This document defines the interface and the invariants it
enforces. **No implementation exists yet.** Nothing here should be built against until this
document itself is reviewed and any capability it grants the broker — intentional or accidental —
is confirmed to be exactly what was meant.

## Why this exists

`useradd`/`userdel` and cgroup management no longer require the broker to be root (see
`scripts/README.md`, `HISTORY.md`'s "Privilege separation for useradd/userdel" entry). The one
remaining piece: spawning the artifact process under its target ephemeral uid. `preexec_fn`
calling `os.setuid()` requires the *forking* process to already be root — a hard Linux constraint.
`siphonophore-spawn` is the minimal, narrowly privileged helper that performs exactly that step on
the broker's behalf, so the broker's own Python interpreter never needs to hold uid 0.

## The one property this whole contract exists to protect

**The broker supplies only bounded data and a target identity. The helper alone owns execution
semantics** — which interpreter, what flags, what environment, what groups, what cwd. The moment
the broker can specify any of those, `siphonophore-spawn` stops being a narrowly privileged
mechanism and becomes a generic root-execution proxy with a nicer protocol wrapped around it. Every
invariant below is in service of this one property; none of them is optional.

## Actors and flow

```
unprivileged broker
      │  creates two anonymous pipes itself (never asks the helper to open a path)
      │  writes: control envelope, then source bytes, then payload bytes, then nonce bytes
      │  (nonce only present for the check-in-gated variant) -- ONE multiplexed stream
      ▼
sudo -n /usr/local/libexec/siphonophore-spawn      <- exact fixed command, no broker-controlled argv
      │
      ├─ SH-01 read and validate the control envelope (version, declared lengths, uid, username)
      ├─ SH-02 cross-validate identity: uid in range, username matches convention,
      │        getpwnam(username).pw_uid == uid
      ├─ SH-03 read exactly code_length source bytes, exactly payload_length payload bytes,
      │        exactly nonce_length nonce bytes if present -- capped independently of what the
      │        envelope claims
      ├─ SH-04 construct the sanitized child environment itself (does not trust anything the
      │        broker sent)
      ├─ SH-05 create fresh, separate pipes for source / payload / nonce to hand to the final
      │        runtime (no sudo in this inner hop, so ordinary pass_fds is safe here)
      ├─ SH-06 setgroups([]) / setgid(gid) / setuid(uid)
      ├─ SH-07 close every helper/control fd that isn't one of the three handed to the final
      │        runtime
      ▼
fixed bootstrap runtime, running AS the target uid, privilege already dropped
      │
      └─ reads source / payload / nonce each from its own inherited fd -- none of it ever
         touched argv, anywhere in the chain
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

A small, fixed-shape header — protocol/version and identity/length metadata only. **Never**: an
executable path, argv, environment variables, cwd, groups, interpreter flags, or a shell selector.
**Never** a filesystem path for source/payload/nonce material — see `SH-11`.

```
{"version": 1, "uid": 60017, "username": "sipho-core-a1b2c3d4",
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

**Helper → final runtime (no `sudo` involved at this hop): genuinely separate fds**, one each for
source / payload / nonce, passed the same way `nonce_pipe()`/`read_nonce_from_fd()` already do
today for the check-in nonce specifically (`identity.py`) — extended uniformly to source and
payload rather than staying a special case.

## Pipe ownership (`SH-11`)

**The broker creates every pipe that carries data to the helper, before invoking `sudo`, and
passes only the resulting stdin stream — the helper never opens a broker-named filesystem path to
obtain source, payload, or nonce material.** A "source path" or "payload path" field in the
envelope would be a confused-deputy surface: the unprivileged broker naming an arbitrary path, the
privileged helper reading whatever that path actually points to. There is no such field, and there
must never be one.

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
  or `getpwnam(username).pw_uid != uid`
- `SH-18` any fd-related failure — pipe creation, `pass_fds` setup, or an fd already in an
  unexpected state
- `SH-19` privilege-drop failure (`setgroups`/`setgid`/`setuid` raising) — the helper must not
  proceed to construct or exec the final runtime if any of these three calls fails, and must not
  leave the failure ambiguous (partial drop is not an acceptable state to exec from)

## Ordering invariant (`SH-20`)

**Privilege is dropped, and every helper/control fd not explicitly handed to the final runtime is
closed, before any artifact-controlled instruction executes.** No source byte the broker supplied
is interpreted as code, and no payload byte is exec'd or evaluated, until `SH-06`/`SH-07` have both
completed successfully.

## Liveness invariant (`SH-21`)

A killed helper, a malformed or truncated envelope, or a client that stops sending mid-stream must
not leave the helper blocked indefinitely, nor leave privileged state (an fd, a partially-read
buffer, anything) outstanding. Every read is bounded by both a byte cap (`SH-14`) and a wall-clock
timeout; every exit path — success, any `SH-12`–`SH-19` refusal, or an unexpected exception —
releases whatever the helper itself allocated before that point.

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
  and attack surface into the privileged path). Decide when implementation actually starts, not
  here.
- The fixed bootstrap runtime's own exact shape (a `python -I` invocation reading from an
  inherited fd vs. a tiny purpose-built reader) — constrained by `SH-10`'s fd layout, not yet
  specified further.
- Resource limits (`rlimit`s) beyond the read-size caps already named in `SH-14` — worth deciding
  explicitly, not defaulting silently, but out of scope for freezing the wire contract itself.

## Next step

Review this document. Confirm nothing here grants the broker a capability that wasn't consciously
intended. Only after that: implement, with each `SH-NN` invariant getting its own named test before
this contract's status can move from PINNED (interface frozen) to whatever this project's
convention for "implemented and validated for real" ends up being.
