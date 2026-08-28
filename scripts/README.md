# Privilege separation for siphonophore-core

Lets the broker process run genuinely unprivileged (not root) while still using the
`uid_cgroup`/`uid_cgroup_checkin` execution tiers. Three pieces together achieve this: the two
one-time deployment mechanisms below (`useradd`/`userdel` delegation, cgroup v2 delegation), plus
`spawn_helper/siphonophore-spawn` — a separate, per-execution privileged helper, see
`contracts/spawn_helper.md` — for the one remaining step neither of these two closes (§3 below). See
`HISTORY.md`'s "broker-root-privilege gap" entry for why this exists.

Two separate mechanisms, both one-time deployment setup, neither is per-execution:

## 1. `useradd`/`userdel` — via scoped sudo to two tiny wrapper scripts

`siphonophore-useradd`/`siphonophore-userdel` each validate their own arguments strictly (uid
range, username pattern) before calling the real binary with fixed, hardcoded flags -- nothing
about the actual `useradd` invocation is configurable by the caller. Install the sudo grant from
`siphonophore-sudoers.template` (see that file's own header for the exact commands) so the
broker's own unprivileged user can run *only* these two scripts as root, nothing else.

`execution_uid_cgroup.py` calls these scripts through `_elevation_prefix()` (mirrors
`warden/privilege.py`'s own pattern): `sudo -n` is prepended only when the broker is not already
root, so a broker that legitimately does run as root (a nested/CI context, today's colima test
runs) skips elevation entirely rather than depending on a sudo grant it doesn't need. `-n`: never
prompt -- a hands-off broker blocking on a password is a silent hang, not a useful failure.

Script paths default to `scripts/` inside this checkout; override with the
`SIPHONOPHORE_USERADD_HELPER`/`SIPHONOPHORE_USERDEL_HELPER` environment variables if installed
elsewhere.

## 2. cgroup v2 delegation -- no sudo needed at all

`provision_cgroup()`/`release_cgroup()`/`add_pid_to_cgroup()` are plain filesystem operations
under a cgroup root path (`/sys/fs/cgroup/siphonophore-core` by default). Linux cgroup v2 supports
delegating a subtree by ownership -- if that path is `chown`'d to the broker's own unprivileged
user once, at setup time, the broker can freely manage it without any elevation:

```bash
sudo mkdir -p /sys/fs/cgroup/siphonophore-core
sudo chown broker-user:broker-group /sys/fs/cgroup/siphonophore-core
```

No code change needed for this half -- it's the same code path whether the directory was created
by root ahead of time (then delegated) or is created by a root broker directly, as it is today.

## 3. The remaining piece: `preexec_fn`'s privilege-drop step, closed by `siphonophore-spawn`

The `preexec_fn` privilege-drop step (spawning the artifact process under the target ephemeral uid)
requires the *forking* process to already be root — an unprivileged broker cannot `os.setuid()` to
an arbitrary target uid itself. **This is solved, not open.** `spawn_helper/siphonophore-spawn` (see
`contracts/spawn_helper.md`) is a minimal, narrowly-scoped privileged helper, invoked via an exact,
argument-free `sudo` command, that performs exactly this step on the broker's behalf — the broker's
own Python process never needs to hold uid 0. `SpawnHelperBackend`
(`siphonophore_core/execution_spawn_helper.py`) and `CheckedInSpawnHelperBackend`
(`siphonophore_core/execution_spawn_helper_checkin.py`) wire this into the normal `Executor` dispatch
path for the `uid_cgroup` and `uid_cgroup_checkin` execution classes respectively, alongside (not
replacing) the root-requiring `UidCgroupBackend`/`CheckedInUidCgroupBackend` — a deployment chooses
which backend to register per class.

Add `siphonophore-spawn`'s own sudoers grant (its exact, argument-free invocation is specified in
`contracts/spawn_helper.md`'s "Invocation shape" section) alongside the `useradd`/`userdel` grant
above to let a genuinely unprivileged broker use these execution classes without running as root
itself.

What `siphonophore-spawn` does and does not establish is a separate, important distinction — see
`contracts/spawn_helper.md`'s `SH-23` section: it proves execution-identity consistency and replay
prevention (at most one real spawn per `execution_id`), not that the spawn it was asked to perform
was ever authorized by a real Gate Decision. That check happens one layer up, inside the broker,
before the helper is ever invoked.
