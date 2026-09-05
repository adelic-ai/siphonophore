"""Mediator startup hardening -- closing two real findings from this stage's adversarial review.

Neither finding is a Siphonophore bug, and neither is fixed by changing Siphonophore. Both are
consequences of README.md's Finding 1 (the backend's substrate authority is AMBIENT to its
process), and ambient authority is exactly the thing an attacker influences by influencing the
environment. So the mediator makes the two ambient inputs EXPLICIT before constructing anything.

FINDING H-1: the kubectl binary was resolved by PATH.
`K8sPodBackend` builds `[self._kubectl, ...]` (execution_k8s.py:121-125) and the default value is
the bare name `"kubectl"`, resolved through `PATH`. If M's `PATH` ever contained a directory R can
write, R would supply the "kubectl" that M runs -- with M's credential in the environment. `sudo`'s
`secure_path` normally prevents this, but relying on one sudoers default for the whole trust
boundary is exactly the pattern this experiment exists to distrust. The mediator now REFUSES to run
unless its configured kubectl is an absolute path, and the provisioning spec requires
`secure_path` as well. Two independent barriers, neither sufficient alone.

FINDING H-2: the kubeconfig was located through HOME.
`kubectl` reads `$KUBECONFIG`, else `$HOME/.kube/config`. Under `sudo -u M`, whether `HOME` becomes
M's home or stays R's depends on `always_set_home`/`env_reset`/`-H` and varies between sudo
configurations and distributions. If `HOME` stayed R's, M would consult a kubeconfig R controls.
That does not leak M's credential (R's config carries R's authority, which is none), but it lets R
silently redirect the mediator at a cluster of R's choosing, which would corrupt the experiment
without failing it. The mediator now sets `KUBECONFIG` explicitly from its own M-owned config, so
the credential path stops depending on `HOME` semantics at all.

Also scrubbed: interpreter and loader variables that would be dangerous if `SETENV` were ever
granted by mistake. Defence in depth -- the provisioning spec forbids `SETENV`, and this makes that
prohibition non-load-bearing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Removed from the mediator's environment unconditionally. Every one of these changes what code
# runs or how it is loaded.
_SCRUBBED_ENV = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONOPTIMIZE", "PYTHONWARNINGS",
    "PYTHONEXECUTABLE", "PYTHONUSERBASE", "PYTHONNOUSERSITE",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
    "BASH_ENV", "ENV", "IFS", "CDPATH", "GLOBIGNORE",
    "KUBECONFIG",          # re-set explicitly below from M's own config, never inherited
    "KUBERNETES_MASTER", "KUBECTL_EXTERNAL_DIFF", "KUBE_EDITOR",
)

DEFAULT_SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class HardeningReport:
    scrubbed: tuple[str, ...] = ()
    kubeconfig_set: str | None = None
    home_set: str | None = None
    path_set: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"scrubbed": list(self.scrubbed), "kubeconfig_set": self.kubeconfig_set,
                "home_set": self.home_set, "path_set": self.path_set}


def harden_environment(config, environ: dict[str, str] | None = None) -> HardeningReport:  # noqa: ANN001
    """Make the mediator's two ambient inputs explicit. Mutates `environ` (defaults to os.environ)."""
    env = os.environ if environ is None else environ
    scrubbed = tuple(name for name in _SCRUBBED_ENV if name in env)
    for name in scrubbed:
        del env[name]

    env["PATH"] = config.safe_path or DEFAULT_SAFE_PATH
    kubeconfig = getattr(config, "kubeconfig", None)
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig
    home = getattr(config, "mediator_home", None)
    if home:
        env["HOME"] = home
    return HardeningReport(
        scrubbed=scrubbed,
        kubeconfig_set=env.get("KUBECONFIG"),
        home_set=env.get("HOME") if home else None,
        path_set=env["PATH"],
    )


def deployment_findings(config, *, sys_path: list[str] | None = None,
                        environ: dict[str, str] | None = None) -> list[str]:
    """Preconditions the INSTALLED mediator must satisfy. A non-empty result means the mediator
    refuses to run: a misconfigured fixture must be INCONCLUSIVE, never a quietly weaker run.

    Not applied to in-process unit tests, which call `service.handle_request` directly -- these are
    deployment properties, and asserting them in a test venv would only measure the test venv."""
    env = os.environ if environ is None else environ
    findings: list[str] = []

    if not os.path.isabs(config.kubectl):
        findings.append(
            f"kubectl must be an absolute path, got {config.kubectl!r} (finding H-1: a "
            "PATH-resolved binary is substitutable by anyone who can influence PATH)"
        )
    if not getattr(config, "kubeconfig", None):
        findings.append(
            "kubeconfig must be configured explicitly (finding H-2: otherwise the credential is "
            "located through HOME, whose value under `sudo -u` is configuration-dependent)"
        )
    if "PYTHONPATH" in env:
        findings.append("PYTHONPATH is set; the launcher must pass -I (isolated mode)")

    path = sys_path if sys_path is not None else __import__("sys").path
    if "" in path:
        findings.append(
            "sys.path contains '' (the current working directory, which under `sudo -u` is R's); "
            "the launcher must pass -I"
        )
    return findings
