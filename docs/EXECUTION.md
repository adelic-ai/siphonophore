# Execution requirements

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

Two dimensions are exercised for real on Linux, via `siphonophore_core`'s execution backends:

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

`same_process` and `separate_process` are also implemented, as the two weakest tiers — no distinct
UID or cgroup, used for low-consequence work. Both refuse outright if the broker process itself is
euid 0, unless a caller explicitly opts in (`allow_root=True`) — a deliberate guard against a
low-consequence intent silently inheriting a root broker's full privilege.

## What's architectural direction only, not built

- **Sandbox/namespace, container, VM** — no execution backend for any of these exists. The
  architecture doesn't make any particular substrate part of the authority model, so adding one is
  intended to require no change to what `Order`, `Authority`, `Intent`, or `Decision` mean — but none
  has actually been built or tested.
- **Credentials** — a related, deliberately separate question from execution identity: what machine
  identity or credentials a specific authorized execution needs to act on anything beyond the local
  host (an API call, a cloud resource, a downstream service). The intended property is that
  credentials follow the specific authorized execution rather than being ambient credentials every
  agent in a shared harness inherits merely by running inside it — a narrowly scoped workload might
  eventually receive a SPIFFE/SPIRE-issued identity, a more free-form one short-lived credentials via
  Vault-issued JWTs. Neither technology is committed to; nothing here is implemented today.
- **Filesystem policy, network policy, resource limits** — named as real dimensions this model
  should eventually constrain per-execution, not currently enforced by any Siphonophore component
  beyond whatever the chosen substrate (e.g., a future container backend) would provide natively.

## Platform attestation is a separate, lower layer

Everything above establishes *which process, on this host, did this* on a given occasion. It says
nothing about whether the host itself — the kernel doing the verifying — is trustworthy. That's a
different question, at a different granularity, answered once at broker startup rather than
per-execution. Not designed in detail and not implemented; see `DESIGN.md` §8 for the full
discussion, including who would attest the broker's own integrity — left genuinely open.
