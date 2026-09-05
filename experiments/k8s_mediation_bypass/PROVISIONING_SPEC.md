# Provisioning specification — Kubernetes mediation-bypass experiment

**STATUS: CONTRACT FOR A LATER PRIVILEGED STAGE. NOTHING HERE HAS BEEN PROVISIONED.**

This document is derived from the implemented experiment code in `sipho_bypass/`, not written
speculatively ahead of it. Every requirement below exists because some specific module needs it,
and the module is named. Nothing in this file was created, installed, or configured by the
implementation stage — creating it requires authority that a disposable development VM does not
and should not have.

Read [`README.md`](README.md) (the pre-registration) first. This file only says how to build the
deployment that document describes.

The overriding rule: **if any requirement here is unmet at run time, the experiment is
INCONCLUSIVE, never a pass.** A misconfigured fixture that quietly weakens the test is the failure
mode this whole specification exists to prevent, and `sipho_bypass.mediator.hardening` refuses to
start the mediator rather than run under one.

---

## 1. Identities

| Role | Purpose | Requirement |
|---|---|---|
| **R** — requester | the principal whose reachability is measured | a real, unprivileged Unix user |
| **M** — mediator | holds the Kubernetes credential | a real Unix user, **distinct uid and distinct primary gid from R** |
| **O** — observer | independent evidence | may be a third identity or the provisioner; must NOT be R |

Hard requirements:

- `uid(R) != uid(M)`. If they are equal the experiment cannot distinguish the two authorities and is
  INCONCLUSIVE by the pre-registration's own list.
- R must **not** be in M's primary group, and M's group must not include R. `integrity.mode_is_writable_by`
  treats group membership as write authority, and so does the kernel.
- Neither R nor M needs root. **Do not grant root to either.** The mediator needs to be a *different*
  identity, not a *privileged* one — this is why no root-owned C helper is specified.
- R must **not** be a member of `docker`, `containerd`, `podman`, `crio`, or `lxd`
  (`requester.authority_snapshot.RUNTIME_GROUPS`). Membership is a total bypass: it permits `docker exec`
  into kind's control-plane container and retrieval of `/etc/kubernetes/admin.conf`. This is excluded by
  the threat model **and** measured at run time (falsification case F-11) — provisioning must satisfy it,
  and the experiment must still check.

## 2. Files, ownership and modes

| Path (suggested) | Owner:Group | Mode | Why, and which module needs it |
|---|---|---|---|
| `/opt/sipho-mediation-bypass/` | `root:root` | `0755` | pinned tree root; must not be R-writable at any level |
| `/opt/sipho-mediation-bypass/venv/` | `root:root` | `0755` | isolated interpreter; `siphonophore_core` + `sipho_bypass` installed here |
| `/opt/sipho-mediation-bypass/MANIFEST.json` | `root:root` | `0644` | output of `integrity.build_manifest()`, recorded at install time |
| `/usr/local/libexec/sipho-mediate` | `root:root` | `0755` | the fixed launcher named in the sudoers grant |
| `/etc/sipho-mediation-bypass/mediator.json` | `M:M` | `0640` | `mediator.config.load_config()`; readable by M, **not by R** |
| M's kubeconfig, e.g. `/etc/sipho-mediation-bypass/mediator.kubeconfig` | `M:M` | `0600` | **the property under test.** R must not be able to read this |
| `/etc/sipho-mediation-bypass/connection/` | `root:root` | `0755` | non-secret connection info for R (§5) |
| `/etc/sipho-mediation-bypass/connection/ca.crt` | `root:root` | `0644` | cluster CA — a public trust anchor, deliberately world-readable |
| `/etc/sipho-mediation-bypass/connection/apiserver` | `root:root` | `0644` | API server URL, one line |
| M's evidence dir, e.g. `/var/lib/sipho-mediation-bypass/evidence/` | `M:M` | `0700` | full unredacted M-side records; R must not read these |
| R's evidence root, default `/tmp/sipho-bypass-evidence/` | `R:R` | `0700` | `attempt.DEFAULT_EVIDENCE_ROOT`; outside the repository |

**Ancestor chain rule.** Every directory on the path to the pinned tree and the launcher must be
non-writable by R. A writable parent lets R replace a child regardless of the child's own mode;
`integrity.verify_not_writable_by()` walks the whole chain for exactly this reason and its
`safe_for_privileged_import` property is false if any ancestor fails.

**Verification after install (run as R, not as the provisioner):**

```python
from sipho_bypass import integrity
integrity.verify_not_writable_by("/usr/local/libexec/sipho-mediate", uid=<R uid>, gids=<R gids>)
integrity.verify_not_writable_by("/opt/sipho-mediation-bypass", uid=<R uid>, gids=<R gids>)
integrity.verify_manifest("/opt/sipho-mediation-bypass", json.load(open(".../MANIFEST.json")))
```

Both reports must show `safe_for_privileged_import is True` and the manifest diff must be `ok`.

## 3. The launcher and the sudo grant

The launcher is the only thing R may invoke as M. It takes **no arguments**; the entire request
arrives on stdin.

```sh
#!/bin/sh
# /usr/local/libexec/sipho-mediate  (root:root 0755)
set -eu
exec /opt/sipho-mediation-bypass/venv/bin/python -I -m sipho_bypass.mediator
```

`-I` (isolated mode) is required, not optional. It implies `-E` (ignore `PYTHONPATH`) and `-s` (no
user site-packages), and on Python ≥ 3.11 also `-P` (do not prepend the cwd). Under `sudo -u M` the
current working directory is still **R's**, so without `-I` an `''` entry on `sys.path` would be a
directory R controls — a direct R-controlled import source into the privileged mediator.
`hardening.deployment_findings()` refuses to start if `''` is on `sys.path` or `PYTHONPATH` is set.

Sudoers drop-in, installed with `visudo -f`, mode `0440`:

```
# /etc/sudoers.d/sipho-mediation-bypass
Defaults!SIPHO_MEDIATE   env_reset, secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Cmnd_Alias SIPHO_MEDIATE = /usr/local/libexec/sipho-mediate ""
<R> ALL=(<M>) NOPASSWD: SIPHO_MEDIATE
```

Requirements, each load-bearing:

- **Run-as is M, not root** (`ALL=(<M>)`). The mediator must never be privileged.
- **The trailing `""` means "invocable with no arguments at all"** — the SH-08 convention already
  documented in `scripts/siphonophore-sudoers.template` and independently re-validated by Stage 2
  (`sudo -n capture-30 ""` was refused; the bare form was accepted). **Verify this with `sudo -n -l`
  on the actual sudo version before trusting it** — this repository's own discipline is to validate
  such claims rather than assume them from documentation. `mediator/__main__.py` refuses any argv
  regardless, as a second barrier.
- **No `SETENV`.** R must not be able to set M's environment. `hardening.harden_environment()`
  scrubs the dangerous variables anyway, so this prohibition is not the only defence — but it must
  still be honoured.
- **`env_reset` and `secure_path` are required**, closing adversarial finding H-1 from the
  implementation stage (a PATH-resolved `kubectl` is substitutable by anyone who can influence
  `PATH`). The mediator additionally refuses a non-absolute `kubectl`, so again two barriers.
- **No wildcards, no arbitrary command, no arbitrary path, no interpreter invocation.** Do not grant
  sudo on anything inside `/opt/.../venv/bin/python` directly; that would be arbitrary code execution
  as M.
- Do **not** grant R `sudo -u M` for anything else, ever. That is equivalent to handing R the
  credential.

## 4. Mediator configuration

`/etc/sipho-mediation-bypass/mediator.json`, owned by M, mode `0640`:

```json
{
  "requester_principal_id": "bypass-requester",
  "authorized_kinds": ["run_artifact"],
  "namespace": "default",
  "image": "python:3.12-slim",
  "kubectl": "/usr/bin/kubectl",
  "kubeconfig": "/etc/sipho-mediation-bypass/mediator.kubeconfig",
  "mediator_home": "/var/lib/sipho-mediation-bypass",
  "safe_path": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
  "enforce_deployment_hardening": true,
  "evidence_dir": "/var/lib/sipho-mediation-bypass/evidence",
  "timeout_seconds": 180.0
}
```

- `kubectl` **must be absolute** (finding H-1). The mediator refuses to start otherwise.
- `kubeconfig` **must be set explicitly** (finding H-2). Otherwise `kubectl` locates the credential
  through `$HOME`, and whether `sudo -u` sets `HOME` to the target user's home varies with
  `always_set_home`/`env_reset` across sudo configurations and distributions. Setting it here makes
  the credential path independent of that.
- `enforce_deployment_hardening` **must be `true`** in any provisioned deployment. It is `false` by
  default so the cluster-free unit tests do not assert properties of a test venv.
- `consequence` and `execution_class` are **not configurable and not accepted** — they are fixed to
  `k8s`/`k8s_pod` in code. `ConsequencePolicy.evaluate()` maps an unknown consequence to
  `same_process` (`policy.py:86`), which would execute R's `artifact_code` inside M's process and
  hand R the credential. Three independent barriers prevent it (protocol `FORBIDDEN_FIELDS`,
  mediator-side config, and registering only the `k8s_pod` backend); do not add a fourth path by
  making them configurable.

## 5. Kubernetes

- **Cluster:** a local `kind` cluster, as in Stage 1 and Stage 2. No managed cluster; the
  pre-registration's non-claims carry Stage 2's topology limits forward unchanged.
- **API-server audit logging enabled**, same shape as Stage 1's `kind/audit-policy.yaml`
  (`pods` at `level: Metadata`). Criterion 5 is unevaluable without it.
- **Audit log readable by O.** If the observer cannot read it, the run is INCONCLUSIVE ("the audit
  observer is unavailable before the hypothesis is exercised").
- **M's credential:** the kubeconfig above. In the minimum configuration this may be kind's default
  admin credential. **Record that fact:** the mediator is then cluster-admin, and the resulting claim
  must not be phrased as though M were narrowly scoped. Narrow RBAC for M is the first follow-on, not
  a prerequisite (README.md, "Why this mechanism, and not the alternatives").
- **M's Kubernetes principal must be MEASURED, not assumed.** Run
  `observer.live_state.measured_mediator_principal()` (which shells `kubectl auth whoami`) during
  preflight and record the result in the attempt's evidence. `kubernetes-admin` is deliberately not a
  default anywhere in the code, and a test asserts it appears in no string constant. In Stage 2 both
  identities were `kubernetes-admin` and the audit `user.username` field carried no discriminating
  information; that conflation is exactly what criterion 5 exists to remove.
- **R's connection-only material** (see PRE-EXECUTION IMPLEMENTATION CLARIFICATION 1 in README.md):
  the API server URL and the cluster CA certificate, world-readable, containing **no** client
  certificate, **no** client key, **no** bearer token, and **no** kubeconfig `users:` stanza. This
  grants cluster *location* and a *trust anchor* only. Without it, a rejected direct-API attempt
  cannot be distinguished from a failure to reach the server at all, and criterion 2 would be
  unevaluable.
- **R may have the `kubectl` binary** (PRE-EXECUTION IMPLEMENTATION CLARIFICATION 2). A binary is not
  a credential. Without it, bypass case B fails with `kubectl_binary_missing`, which the classifier
  correctly marks INCONCLUSIVE — tooling absence would mask the authority question rather than
  answer it.
- **Do NOT pre-disable ServiceAccount auto-mounting.** `K8sPodBackend` does not set
  `automountServiceAccountToken: false`, and falsification case F-05 exists to measure what that
  actually implies on this cluster. Disabling it during provisioning would delete the highest-value
  case in the pre-registration.
- **Do not pre-grant the `default` ServiceAccount any RBAC.** Leave the cluster's default bindings
  as they are and let F-05 measure them.

## 6. Environment

| Variable | Requirement |
|---|---|
| `KUBECONFIG` | must be **unset for R**. Set for M explicitly by `hardening.harden_environment()` from M's config, never inherited |
| `HOME` | set for M from `mediator_home`; R's `HOME` must contain no `.kube/config` |
| `PATH` | reset by `secure_path` in sudoers **and** replaced by `hardening.harden_environment()` |
| `PYTHONPATH` | must be unset; the launcher's `-I` ignores it and the mediator refuses to start if it is present |
| interpreter/loader vars | `PYTHONHOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `BASH_ENV`, `IFS`, … are scrubbed unconditionally (`hardening._SCRUBBED_ENV`) |

R's home must contain no `~/.kube/config`. `requester.authority_snapshot` measures this rather than
assuming it (F-03).

## 7. Runtime authority

R must have **no** container-runtime authority: not in the runtime groups (§1), and unable to read
any of `hardening`-adjacent sockets enumerated in `authority_snapshot.RUNTIME_SOCKETS`
(`/var/run/docker.sock`, `/run/containerd/containerd.sock`, `/run/crio/crio.sock`,
`/run/podman/podman.sock`, and the `/run` variants). Detection tolerates the tooling being entirely
absent, which is the expected state on a host that only runs kind through a separate provisioner.

## 8. Observation

- O queries live state with its **own** kubeconfig (`observer.live_state.ObserverAccess`), never R's
  absence of one and never by reading `Effect.detail`.
- O reads the audit log and parses it with AgentWatch's unmodified
  `agentwatch.groundtruth.k8s_audit.parse_lines`, reached by `AGENTWATCH_REPO` pointing at a sibling
  checkout — the same external, test-only pattern Stage 1 and Stage 2 used. AgentWatch is **not** a
  dependency, appears nowhere in `pyproject.toml`, and is not modified.
- **Preflight self-check, required:** run `observer.audit_support.principal_attribute_report()`
  against one real parsed event and confirm `usable is True`. Stage 1 read the principal but never
  had to select on it, so the attribute name carrying `user.username` is treated here as an
  assumption to verify rather than a fact to rely on. If it is unusable, criterion 5 cannot be
  evaluated and the run is INCONCLUSIVE.
- Record the pinned AgentWatch commit in the attempt's provenance
  (`attempt.AttemptDirectory.provenance(agentwatch_repo=...)`).

## 9. Cleanup, in this order

Evidence first, always — a preserved attempt is the point of the run.

1. Copy R-side and M-side evidence directories to durable storage. **Never overwrite an existing
   attempt**; `attempt.AttemptDirectory.create()` refuses a collision by design.
2. Confirm no raw credential was written anywhere: every artifact passed
   `redaction.assert_no_secrets` at write time, so this is a re-check, not the primary control.
3. Delete the cluster (`kind delete cluster`).
4. Remove M's kubeconfig and any connection material.
5. Remove `/etc/sudoers.d/sipho-mediation-bypass` (validate with `visudo -c` afterwards).
6. Remove `/usr/local/libexec/sipho-mediate` and `/opt/sipho-mediation-bypass/`.
7. Remove the M and R accounts if they were created for this experiment.
8. Take a final `authority_snapshot` **before** teardown, not after — criterion 8 compares R's
   authority at the start and end of the experiment, and teardown changes it by design.

## 10. Why this cannot be provisioned from the development VM

Every item above requires authority the implementation environment deliberately lacks, and should
lack:

- creating Unix identities, and writing to `/etc/sudoers.d/`, `/usr/local/libexec/`, `/opt/` —
  root on the target host;
- creating and holding a Kubernetes credential — cluster authority;
- running a container runtime and a `kind` cluster — Docker/containerd authority.

More importantly, **R must not be able to grant itself M's capability.** That is the property under
test. An experiment in which the requester provisions its own mediator measures nothing, so
provisioning by a separate authorized identity is a legitimate authority boundary rather than an
inconvenience — the same shape Stage 2's `sipho-stage2` helpers and
`scripts/siphonophore-sudoers.template` already prescribe.
