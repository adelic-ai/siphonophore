"""The M -> R response. Defines exactly what crosses back, and nothing else may.

Pre-registration mapping: falsification case F-10 ("copying the mediator credential via the
helper's own output, stdout/stderr, or an error message") and criterion 7 ("R never receives and
cannot read M's substrate credential at any point, including after a successful mediated
execution").

The rule this module exists to enforce: the response is CONSTRUCTED from a whitelist, never
FILTERED from a larger object. A filter has to anticipate every leak; a whitelist has to anticipate
every need, and failing to anticipate a need is a visible bug rather than a silent disclosure.

Error normalization: an exception's message is never passed through. Siphonophore's own exception
strings legitimately embed mediator-side state -- `ProvisioningError` embeds kubectl's stderr,
which can contain the kubeconfig path and the API server URL (execution_k8s.py:56-58);
`ExecutionError` embeds the Pod's logs and name (execution_k8s.py:205). Instead the mediator emits
a stable `error_class` plus a `error_detail` dict built ONLY from values the mediator already knew
before the exception existed (its own configured `authorized_kinds`, the validated request `kind`).
The full exception is preserved in M-side evidence, where R cannot read it.

Consequence of that choice, stated rather than hidden: on `execution_failed` R learns the class and
nothing more -- not the phase, not the exit code, because those exist only inside the exception
string. That is deliberate. The independent K-live and K-audit channels are where the experiment is
supposed to learn what happened to the Pod anyway; asking M to narrate it back to R would be
self-report standing in for independent observation, which the pre-registration's evidence model
forbids.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .protocol import MAX_STDOUT_BYTES, PROTOCOL_VERSION

# Stable, enumerated error classes. Tests and the evidence model assert on these strings.
ERR_PROTOCOL = "protocol_error"
ERR_GATE_VIOLATION = "gate_violation"
ERR_ARTIFACT_MISMATCH = "artifact_mismatch"
ERR_EXECUTION_FAILED = "execution_failed"
ERR_PROVISIONING_UNAVAILABLE = "provisioning_unavailable"
ERR_INTERNAL = "internal_error"

ERROR_CLASSES = frozenset({
    ERR_PROTOCOL, ERR_GATE_VIOLATION, ERR_ARTIFACT_MISMATCH,
    ERR_EXECUTION_FAILED, ERR_PROVISIONING_UNAVAILABLE, ERR_INTERNAL,
})

# `provisioning_unavailable` means M could not reach its own cluster. Per the pre-registration's
# INCONCLUSIVE list ("the cluster cannot be created or is unreachable") this must never be read as
# a bypass-resistance result in either direction.
INCONCLUSIVE_ERROR_CLASSES = frozenset({ERR_PROVISIONING_UNAVAILABLE, ERR_INTERNAL})

# Exactly the keys lifted out of Effect.detail. `node_name` and `namespace` are omitted from the
# copy of Effect.detail that R sees: R has no need for cluster topology, and the whitelist is the
# whole point. `stdout` IS included, deliberately -- it is the channel F-05 exists to test.
_EFFECT_DETAIL_WHITELIST = ("pod_name", "phase", "exit_code")


@dataclass(frozen=True)
class MediationResponse:
    protocol_version: int = PROTOCOL_VERSION
    status: str = "error"                       # "ok" | "error"
    request_label: str | None = None
    intent_id: str | None = None
    execution_class: str | None = None
    backend_invocations: int | None = None
    pod_name: str | None = None
    phase: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stdout_truncated: bool = False
    error_class: str | None = None
    error_detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bound_stdout(raw: Any) -> tuple[str | None, bool]:
    if raw is None:
        return None, False
    text = raw if isinstance(raw, str) else str(raw)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_STDOUT_BYTES:
        return text, False
    return encoded[:MAX_STDOUT_BYTES].decode("utf-8", errors="ignore"), True


def success(
    *, request_label: str | None, intent_id: str, execution_class: str,
    backend_invocations: int, effect_detail: dict[str, Any],
) -> MediationResponse:
    """Build an ok response by whitelist from `Effect.detail`."""
    picked = {k: effect_detail.get(k) for k in _EFFECT_DETAIL_WHITELIST}
    stdout, truncated = _bound_stdout(effect_detail.get("stdout"))
    exit_code = picked["exit_code"]
    if exit_code is not None and not isinstance(exit_code, int):
        exit_code = None
    phase = picked["phase"] if isinstance(picked["phase"], str) else None
    pod_name = picked["pod_name"] if isinstance(picked["pod_name"], str) else None
    return MediationResponse(
        status="ok",
        request_label=request_label,
        intent_id=intent_id,
        execution_class=execution_class,
        backend_invocations=backend_invocations,
        pod_name=pod_name,
        phase=phase,
        exit_code=exit_code,
        stdout=stdout,
        stdout_truncated=truncated,
    )


def failure(
    *, error_class: str, request_label: str | None = None, intent_id: str | None = None,
    backend_invocations: int | None = None, error_detail: dict[str, Any] | None = None,
) -> MediationResponse:
    if error_class not in ERROR_CLASSES:
        raise ValueError(f"unknown error_class {error_class!r}")
    return MediationResponse(
        status="error",
        request_label=request_label,
        intent_id=intent_id,
        backend_invocations=backend_invocations,
        error_class=error_class,
        error_detail=dict(error_detail or {}),
    )
