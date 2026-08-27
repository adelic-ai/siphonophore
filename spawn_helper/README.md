# `siphonophore-spawn` — build and install

Implements `contracts/spawn_helper.md` (PINNED, `SH-01`..`SH-26`). Read that document first --
this file is only the mechanics of building and deploying what it defines. **Status: built,
compiled locally; not yet validated for real on colima.** Do not treat anything here as proven
until that validation has actually been run and recorded in `HISTORY.md`, per this project's own
discipline.

## Build

```bash
make -C spawn_helper
```

Produces `spawn_helper/siphonophore-spawn`, a single dependency-free binary (libc only).

## Install

**Must be run as root.** This is not optional deployment polish -- `SH-26` requires the helper
binary and the bootstrap runtime it exec's to be root-owned and not writable by the broker's own
user, or the whole privilege-separation exercise is defeated by a broker that can simply overwrite
the "privileged" binary sudo is about to run.

```bash
sudo make -C spawn_helper install
```

Installs:
- `/usr/local/libexec/siphonophore-spawn` (mode `0711`, root:root) -- the helper itself.
- `/usr/local/libexec/siphonophore-bootstrap.py` (mode `0755`, root:root) -- the fixed bootstrap
  runtime `SH-24` hands source/payload/nonce to.

Both paths are hardcoded in `siphonophore-spawn.c` (`BOOTSTRAP_PYTHON`, `BOOTSTRAP_SCRIPT`) --
installing elsewhere requires rebuilding with those constants changed, not a config file, by
design (`SH-26`: no broker-influenced path resolution).

## Sudoers grant

Add the `SIPHONOPHORE_SPAWN` block from `scripts/siphonophore-sudoers.template` to the same
`/etc/sudoers.d/siphonophore-core` file the `useradd`/`userdel` grant already lives in (see
`scripts/README.md`). Validate with `visudo -c` before trusting it, as always in this project.

**Verify the argument-free restriction for real** (`sudo -l` as the broker user, then attempt
`sudo -n /usr/local/libexec/siphonophore-spawn extra-arg` and confirm it's refused) -- the `""`
syntax is documented sudoers behavior, but this project's discipline is to prove claims like this
on the actual deployed sudo version, not assume them from a template comment.

## cgroup root: do NOT delegate it for this path

`scripts/README.md` §2 documents `chown`-delegating `/sys/fs/cgroup/siphonophore-core` to the
broker's own user, so an unprivileged broker calling `provision_cgroup()`/`add_pid_to_cgroup()`
directly (the existing `UidCgroupBackend` path, which still needs the broker to be real root
regardless of delegation) can manage cgroups without further elevation.

**That delegation is specifically for a broker running `UidCgroupBackend` directly. A broker using
only `siphonophore-spawn` should NOT have that same subtree delegated to it.** `SH-23`'s cgroup-leaf
creation happens entirely inside the helper, while it is still real root via `sudo` -- it needs no
delegated write access to `CGROUP_ROOT` at all. If the same subtree is *also* chowned to the
broker's user for some other reason, the broker regains exactly the independent leaf-creation
capability the trust-boundary discussion in `contracts/spawn_helper.md`'s `SH-23` section already
names as a limit on what this helper can prove -- delegating unnecessarily doesn't break anything
`SH-23` claims (it never claimed to stop that), but it does make the deployment's own trust
boundary murkier than it needs to be for no benefit, if `siphonophore-spawn` is the only thing
touching that subtree. Leave `CGROUP_ROOT` root-owned (`root:root`, mode `0755` is sufficient --
the helper creates leaves as root) for a deployment that uses `siphonophore-spawn` exclusively.

## What "install" does not include

- Wiring `siphonophore-spawn` into an `ExecutionBackend` that `Executor` actually dispatches to --
  that integration is separate, later work; this directory only builds and installs the mechanism
  `contracts/spawn_helper.md` defines.
- Validating any of `SH-01`..`SH-26` for real against a running instance. See `HISTORY.md` for
  whether/when that's been done -- do not assume it from this README existing.
