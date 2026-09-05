# Execution requirements

Substrate-neutral by design: nothing in this document is specific to one execution substrate. The
Kubernetes realization of the model described here lives in [`EXECUTION_K8S.md`](EXECUTION_K8S.md);
Linux/local realizations are described inline below because they are what the core repository ships
by default, not because the model is Linux-shaped.

## The path an execution takes

```
request / intent            an attempted exercise of authority (never its source)
        ↓
authority / decision        Gate re-verifies the Authority, Policy evaluates, a Decision is minted
  / mediation               with kind, execution class, and artifact digest bound into it
        ↓
executor / substrate        Executor independently re-verifies the Decision and artifact, then
  selection                 resolves the bound execution class to a registered backend
        ↓
── substrate boundary ──    ExecutionBackend (DESIGN.md §10)
        ↓
concrete backend            whatever it actually takes on one substrate -- examples, one per
                            substrate: a process, a UID and cgroup, a Kubernetes Pod
        ↓
effect                      plus whatever evidence that substrate makes available
```

Every stage above the boundary is the same regardless of what executes below it. That is the
invariant, and `DESIGN.md` §10 is its authoritative statement — including the precise limits of what
"mediation" establishes and what the surrounding deployment has to supply instead.

## Execution requirements are dimensions, not a ladder

Siphonophore treats execution requirements as a set of independent dimensions, not a single
weakest-to-strongest ladder every agent or action climbs:

    execution context
      ├── process / PID lineage
      ├── cgroup
      ├── UID / GID
      ├── sandbox / namespace
      ├── container / VM
      ├── credentials
      ├── filesystem policy
      ├── network policy
      └── resource limits

A specific authorized action needs whatever combination of these its actual risk profile requires —
not the maximum available, and not a fixed tier assigned once to whichever agent happens to be
performing it. A child agent requiring stronger isolation than its parent for one task hasn't
received more authority; it's doing work with a different risk profile. See `DESIGN.md` §2 for why
isolation strength must never be treated as a proxy for delegated authority — an early design mistake
this project made and corrected.

Whichever dimensions a policy selects for a given intent, the resulting execution class is
cryptographically bound into the same `Decision` as the authorization itself (`DESIGN.md` §9's
discipline, applied here). An authorized execution requirement can't be silently substituted for a
different one after authorization — every field execution dispatch branches on has to be part of
what was actually authorized, with no exceptions.

## What's implemented today

Two of the dimensions above are exercised for real, on the Linux/local substrate, via
`siphonophore_core`'s execution backends:

- **UID/GID** — the `uid_cgroup` and `uid_cgroup_checkin` execution classes provision a genuine,
  ephemeral system user per execution (`provision_ephemeral_user()`/`release_ephemeral_user()`),
  dropped into by the spawned process rather than merely labeled.
- **cgroup** — a real cgroup v2 leaf per execution (`provision_cgroup()`), confirmed by process
  membership while running, not just requested.

Two backends implement this: `UidCgroupBackend` (root-requiring, `preexec_fn`-based) and
`SpawnHelperBackend` (unprivileged-broker-compatible, a client of the pinned `siphonophore-spawn` C
helper — see `contracts/spawn_helper.md`). Both are wired into the normal `Executor` dispatch path; a
deployment chooses which one to register, and `Gate`/`Executor`/`Decision` don't distinguish between
them.

`uid_cgroup_checkin` additionally requires the spawned process to complete a kernel-verified check-in
(`SO_PEERCRED`) before anything it did is trusted — see [`EVIDENCE.md`](EVIDENCE.md) for how that
evidence is reconciled.

`same_process` and `separate_process` are also implemented, as the two classes that provision no
distinct execution identity at all — no distinct UID or cgroup, used for low-consequence work. Both refuse outright if the broker process itself is
euid 0, unless a caller explicitly opts in (`allow_root=True`) — a deliberate guard against a
low-consequence intent silently inheriting a root broker's full privilege.

## The substrate boundary, and the substrates behind it

`ExecutionBackend` is the boundary (`DESIGN.md` §10). A substrate is added by implementing it and
registering the implementation under an execution class — not by teaching anything above the
boundary about that substrate.

| Substrate | Execution classes | What it establishes |
|---|---|---|
| Linux / local | `same_process`, `separate_process` | real process separation at most; no distinct execution identity |
| Linux / OS identity | `uid_cgroup`, `uid_cgroup_checkin` | a real ephemeral UID and cgroup v2 leaf per execution; `uid_cgroup_checkin` additionally establishes that identity *through the kernel*, independently of the executing process |
| Kubernetes | `k8s_pod` | a real Pod per execution, correlatable after the fact by label; no check-in tier, so no independently-established execution identity |

Linux/local is a first-class substrate, not a fallback: it is currently the only one where execution
identity is established independently of the process claiming it. Kubernetes is a first-class
substrate too, and the one that confirmed the boundary holds for a second, differently-shaped
substrate — its full realization, tested scope, and limitations are in
[`EXECUTION_K8S.md`](EXECUTION_K8S.md), which is where substrate vocabulary belongs.

That confirmation is checked rather than asserted: `tests/test_core_no_k8s_vocabulary.py` scans every
module in `siphonophore_core` except the one backend allowed to know its own substrate's vocabulary,
and fails if substrate-specific nouns leak upward — see `DESIGN.md` §10 for the two layers it uses
and why both are needed.

## What a backend does, and does not, check

`Executor.execute()` verifies decision↔intent correspondence, the `Decision`'s HMAC, its
`permitted` flag, and the artifact digest, in that order, **before** any backend is looked up. A
backend receives an already-verified `Decision` and re-checks none of it. No backend here reads more
than one field from it: the substrate-identity backends and `K8sPodBackend` read `decision.intent_id`
as their execution correlation identity, and `same_process`/`separate_process` read no `Decision`
field at all.

Two consequences, both deliberate:

- **Decision authenticity is an `Executor`/`Gate` property, never a per-backend one.** Do not read
  any backend's behaviour as independent authorization; there is exactly one layer that authenticates
  a `Decision`, and it is above the boundary.
- **Backends consume ambient substrate authority.** No backend in this repository holds a credential
  of its own; each acts with whatever authority its calling process already has. That is the
  Credentials dimension below, unbuilt, seen from the other side — and it is why substrate-authority
  custody is a deployment property rather than an SDK one (`DESIGN.md` §10).

`decision.intent_id` doubling as execution correlation identity is a convention, not a designed
`execution_id` concept. `DESIGN.md`'s open questions record it as open; nothing here depends on it
being resolved.

## What's architectural direction only, not built

- **Sandbox/namespace, VM** — no execution backend for either exists. The architecture doesn't make
  any particular substrate part of the authority model, so adding one is intended to require no
  change to what `Order`, `Authority`, `Intent`, or `Decision` mean — the Kubernetes substrate is the
  first to actually confirm that for a second, differently-shaped backend; VM and namespace/sandbox-only
  substrates remain unbuilt and untested.
- **Credentials** — a related, deliberately separate question from execution identity: what machine
  identity or credentials a specific authorized execution needs to act on anything beyond the local
  host (an API call, a cloud resource, a downstream service). Candidate mechanisms were considered —
  a SPIFFE/SPIRE-issued workload identity for narrowly-scoped work, short-lived Vault-issued JWTs for
  more free-form work — but neither was committed to. Nothing here ties credential delivery to a
  specific authorized execution today; ambient credentials are whatever the executing process
  already happens to hold.
- **Filesystem policy, network policy, resource limits** — named as real dimensions this model
  should eventually constrain per-execution, not currently enforced by any Siphonophore component
  beyond whatever the chosen substrate would provide natively. A substrate offering such a control
  natively does not make it a Siphonophore guarantee until something above the boundary selects it
  and binds it into the `Decision`.

## Platform integrity is a separate, lower layer

At its strongest — the `uid_cgroup_checkin` class on the Linux/local substrate — everything above
establishes *which process, on this host, did this* on a given occasion. It says nothing about
whether the host itself, or the kernel doing the verifying, is trustworthy — a different question,
at a different granularity, that this project does not attempt to solve. See `DESIGN.md` §8. A
substrate with no check-in tier establishes correspondingly less, and `DESIGN.md` §10's table is
where each layer's actual reach is stated rather than assumed uniform.
