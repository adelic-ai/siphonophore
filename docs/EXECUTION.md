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

## Kubernetes: a first container-substrate backend (experimental, `explore/k8s-substrate-demo`)

`K8sPodBackend` (`siphonophore_core/execution_k8s.py`, execution class `k8s_pod`) runs
`intent.artifact_code` as a real Pod on a real cluster (proven against `kind`; the same shape is
expected, not yet proven, against a managed cluster). It is exactly the extension point this file
described before it existed: a new `ExecutionBackend`, registered under its own execution class,
touching nothing in `execution.py`/`mediation.py`/`policy.py`/`Broker`/`CognitiveLoop`. Full detail,
including what it deliberately does not attempt: [`EXECUTION_K8S.md`](EXECUTION_K8S.md).

This confirms, not merely asserts, that adding a substrate requires no change to what `Order`,
`Authority`, `Intent`, or `Decision` mean — `tests/test_core_no_k8s_vocabulary.py` scans every other
file in `siphonophore_core` for Kubernetes-specific vocabulary (`Pod`, `Job`, `Namespace`,
`ServiceAccount`, `Kubernetes`, `kubectl`, `k8s`) and fails if any leaks in.

## What's architectural direction only, not built

- **Sandbox/namespace, VM** — no execution backend for either exists. The architecture doesn't make
  any particular substrate part of the authority model, so adding one is intended to require no
  change to what `Order`, `Authority`, `Intent`, or `Decision` mean — Kubernetes (above) is the first
  substrate to actually confirm that for a container-shaped backend; VM and namespace/sandbox-only
  tiers remain unbuilt and untested.
- **Credentials** — a related, deliberately separate question from execution identity: what machine
  identity or credentials a specific authorized execution needs to act on anything beyond the local
  host (an API call, a cloud resource, a downstream service). Candidate mechanisms were considered —
  a SPIFFE/SPIRE-issued workload identity for narrowly-scoped work, short-lived Vault-issued JWTs for
  more free-form work — but neither was committed to. Nothing here ties credential delivery to a
  specific authorized execution today; ambient credentials are whatever the executing process
  already happens to hold.
- **Filesystem policy, network policy, resource limits** — named as real dimensions this model
  should eventually constrain per-execution, not currently enforced by any Siphonophore component
  beyond whatever the chosen substrate (e.g., a future container backend) would provide natively.

## Platform integrity is a separate, lower layer

Everything above establishes *which process, on this host, did this* on a given occasion. It says
nothing about whether the host itself, or the kernel doing the verifying, is trustworthy — a
different question, at a different granularity, that this project does not attempt to solve. See
`DESIGN.md` §8.
