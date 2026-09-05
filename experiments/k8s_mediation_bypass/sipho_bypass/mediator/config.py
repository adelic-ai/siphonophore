"""Mediator-side configuration -- every value R must NOT be able to choose.

Pre-registration mapping: the authority topology's "The mediator constructs the Siphonophore
objects that belong on the mediator side."

Loaded from a fixed, M-owned file path passed explicitly by the entry point. Deliberately NOT from
environment variables: `sudo` is specified without `SETENV` (PROVISIONING_SPEC.md), so R cannot set
M's environment today, but reading configuration from the environment would make that sudoers
detail load-bearing for the whole trust boundary. A file M owns is a boundary the OS enforces
directly.

`consequence` is the field that matters most. `ConsequencePolicy.evaluate()` maps an unrecognized
consequence to `same_process` (policy.py:86), and `SameProcessBackend` runs artifact code via
`exec()` in the calling process (execution.py:90). A requester able to influence `consequence`
would therefore be able to execute arbitrary code AS M, and so obtain M's Kubernetes credential --
a total defeat of the property under test. It is fixed here, it is on the protocol's
FORBIDDEN_FIELDS list, and the mediator additionally registers only the k8s_pod backend. Three
independent barriers, because one typo in any single one of them would end the experiment.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/sipho-mediation-bypass/mediator.json"

_ALLOWED_CONFIG_KEYS = frozenset({
    "requester_principal_id", "order_issuer", "authorized_kinds", "namespace",
    "image", "kubectl", "kubectl_context", "timeout_seconds", "evidence_dir",
    "kubeconfig", "mediator_home", "safe_path", "enforce_deployment_hardening",
})


class ConfigError(RuntimeError):
    """The mediator's own configuration is missing or malformed. Always an M-side fixture problem,
    never something R can cause, and always INCONCLUSIVE for the experiment."""


@dataclass(frozen=True)
class MediatorConfig:
    # Identity the mediator submits on behalf of. NOT caller-supplied: Gate.submit() requires
    # intent.principal_id == authority.principal_id (mediation.py:99-100), so letting R choose this
    # would let R name a principal M did not intend to speak for.
    requester_principal_id: str = "bypass-requester"
    order_issuer: str = "sipho-mediation-bypass-experiment"

    # The narrowest authority this experiment can run on: exactly one kind, zero further
    # delegation. `write_file` is deliberately EXPRESSIBLE by the protocol and NOT authorized here,
    # so the real mediation path's refusal is observable.
    authorized_kinds: tuple[str, ...] = ("run_artifact",)

    # Fixed. See module docstring.
    consequence: str = "k8s"
    execution_class: str = "k8s_pod"

    namespace: str = "default"
    image: str = "python:3.12-slim"

    # Absolute path in any real deployment -- see hardening.py, finding H-1. The bare-name default
    # exists only so the cluster-free unit tests need no filesystem fixture; the installed mediator
    # refuses to start with it (`enforce_deployment_hardening`).
    kubectl: str = "kubectl"
    kubectl_context: str | None = None

    # M's Kubernetes credential, named explicitly rather than discovered through HOME -- finding
    # H-2. Set into KUBECONFIG at startup by hardening.harden_environment().
    kubeconfig: str | None = None
    mediator_home: str | None = None
    safe_path: str | None = None

    # Installed deployments set this true; the unit tests leave it false, because these are
    # properties of a provisioned host and asserting them in a test venv would measure the venv.
    enforce_deployment_hardening: bool = False

    timeout_seconds: float = 180.0

    # M-owned directory for the full, unredacted M-side record. Never returned to R.
    evidence_dir: str | None = None

    def __post_init__(self) -> None:
        if not self.authorized_kinds:
            raise ConfigError("authorized_kinds must not be empty")
        if self.consequence != "k8s" or self.execution_class != "k8s_pod":
            raise ConfigError("consequence/execution_class are fixed for this experiment")

    @property
    def policy_mapping(self) -> dict[str, str]:
        return {self.consequence: self.execution_class}


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MediatorConfig:
    """Read M's configuration file. Unknown keys are rejected rather than ignored, for the same
    reason the wire protocol rejects them: a silently-ignored key is a configuration that does not
    do what it appears to."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"mediator configuration not found at {p}")
    try:
        raw: Any = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"mediator configuration at {p} is unreadable or malformed") from exc
    if not isinstance(raw, dict):
        raise ConfigError("mediator configuration must be a JSON object")
    unknown = sorted(set(raw) - _ALLOWED_CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"unknown mediator configuration keys: {unknown}")
    if "authorized_kinds" in raw:
        kinds = raw["authorized_kinds"]
        if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
            raise ConfigError("authorized_kinds must be a list of strings")
        raw["authorized_kinds"] = tuple(kinds)
    return MediatorConfig(**raw)
