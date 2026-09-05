"""The mediator core: one validated request in, one bounded response out.

Pre-registration mapping: the positive case ("mediated ALLOW"), criterion 4, and the design
decision "The mediator accepts an `Intent` (and optionally an `Authority`). It never accepts a
`Decision`."

What this module does NOT do, deliberately:

- It does not reimplement any part of `Gate` or `Executor`. It constructs them and calls
  `Broker.dispatch()`, which is the real path (broker.py:37-39).
- It does not bypass `Executor`. There is no call to `K8sPodBackend.run()` anywhere in this file.
- It does not introduce a second authorization system. The only authorization decisions are made
  by `Gate`/`ConsequencePolicy`, unchanged.

Three independent barriers stop a requester from selecting in-process execution (which would be
arbitrary code execution as M):
  1. `consequence` and `execution_class` are on the protocol's FORBIDDEN_FIELDS list;
  2. the mediator supplies both from its own config, never from the request;
  3. the `Executor` is constructed with ONLY the k8s_pod backend registered, so any other
     execution_class raises GateViolation at execution.py:156-158 before a backend exists to run.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable

from siphonophore_core.execution import (
    ArtifactMismatchError, ExecutionBackend, ExecutionError, Executor,
)
from siphonophore_core.execution_k8s import K8sPodBackend, ProvisioningError
from siphonophore_core.intent import Effect, Intent
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker

from .. import protocol, response as resp
from .config import MediatorConfig


class CountingBackend(ExecutionBackend):
    """Wraps the real backend and counts invocations. Criterion 4 requires "exactly one
    `K8sPodBackend.run()` invocation" -- asserted by counting, never inferred from the absence of
    an exception (the same discipline Stage 1 and Stage 2 used)."""

    def __init__(self, inner: ExecutionBackend) -> None:
        self._inner = inner
        self.invocations = 0

    def run(self, decision, intent) -> Effect:  # noqa: ANN001 -- mirrors ExecutionBackend exactly
        self.invocations += 1
        return self._inner.run(decision, intent)


@dataclass
class MediationOutcome:
    """What the mediator itself keeps. The `response` is the only part that goes to R; `m_side`
    holds the full, unredacted detail (including exception text, which legitimately embeds
    mediator state) for M's own evidence directory."""

    response: resp.MediationResponse
    m_side: dict[str, Any]


def _new_intent_id(label: str | None) -> str:
    """Mediator-generated. R's `request_label` is already validated to `[a-z0-9-]` by the protocol,
    so embedding it is safe -- and the safety does not depend on `pod_name_for()`'s own slugging
    (execution_k8s.py:80-88), which stays a second, independent narrowing rather than the only one."""
    suffix = uuid.uuid4().hex[:8]
    return f"bypass-{label}-{suffix}" if label else f"bypass-{suffix}"


def handle_request(
    raw: bytes,
    config: MediatorConfig,
    *,
    backend_factory: Callable[[MediatorConfig], ExecutionBackend] | None = None,
) -> MediationOutcome:
    """Validate, mediate, and normalize. Never raises for attacker-controlled input.

    `backend_factory` exists so the cluster-free tests can substitute a fake backend. It is NOT a
    caller-supplied parameter in any sense R can reach: `__main__` never passes it, and it is not
    expressible in the wire protocol."""
    m_side: dict[str, Any] = {"stage": "parse"}

    try:
        request = protocol.parse_request(raw)
    except protocol.ProtocolError as exc:
        m_side["protocol_error"] = {"code": exc.code, "detail": exc.detail}
        return MediationOutcome(
            response=resp.failure(error_class=resp.ERR_PROTOCOL, error_detail={"code": exc.code, **exc.detail}),
            m_side=m_side,
        )

    intent_id = _new_intent_id(request.request_label)
    m_side.update({"stage": "dispatch", "intent_id": intent_id, "kind": request.kind})

    # Every security-relevant field is mediator-chosen. The request contributes exactly three
    # things: `kind` (which the Gate then judges), `artifact_code` (data, executed in the Pod), and
    # `payload` (data). Nothing else.
    intent = Intent(
        kind=request.kind,
        principal_id=config.requester_principal_id,
        intent_id=intent_id,
        payload=request.payload,
        consequence=config.consequence,
        artifact_code=request.artifact_code,
    )

    gate = Gate(ConsequencePolicy(mapping=config.policy_mapping, allowed_kinds=tuple(config.authorized_kinds)))
    order = gate.issue_order(
        order_id=f"order-{uuid.uuid4().hex[:8]}",
        issuer=config.order_issuer,
        granted_kinds=frozenset(config.authorized_kinds),
        max_delegation_depth=0,          # narrowest legitimate authority: no onward delegation
    )
    authority = gate.grant_root_authority(order, principal_id=config.requester_principal_id)

    factory = backend_factory or _default_backend
    counting = CountingBackend(factory(config))
    executor = Executor(gate, backends={config.execution_class: counting})   # barrier 3
    broker = Broker(gate, executor)

    error_detail_common = {
        "requested_kind": request.kind,
        "authorized_kinds": list(config.authorized_kinds),
    }

    try:
        effect = broker.dispatch(intent, authority=authority)
    except ArtifactMismatchError as exc:
        # Cannot occur on this path (the mediator hands Executor the same Intent it minted the
        # Decision for), but classified explicitly rather than swept into gate_violation, so that
        # if it ever DOES occur the evidence record says so precisely.
        m_side["exception"] = {"type": type(exc).__name__, "text": str(exc)}
        return MediationOutcome(
            response=resp.failure(
                error_class=resp.ERR_ARTIFACT_MISMATCH, request_label=request.request_label,
                intent_id=intent_id, backend_invocations=counting.invocations,
                error_detail=error_detail_common,
            ),
            m_side=m_side,
        )
    except GateViolation as exc:
        m_side["exception"] = {"type": type(exc).__name__, "text": str(exc)}
        return MediationOutcome(
            response=resp.failure(
                error_class=resp.ERR_GATE_VIOLATION, request_label=request.request_label,
                intent_id=intent_id, backend_invocations=counting.invocations,
                error_detail=error_detail_common,
            ),
            m_side=m_side,
        )
    except ProvisioningError as exc:
        # M cannot reach its own cluster. An M-side fixture failure -- INCONCLUSIVE for the
        # experiment, never a bypass-resistance result. The message embeds kubectl's stderr (which
        # can contain the kubeconfig path and API server URL), so it stays M-side.
        m_side["exception"] = {"type": type(exc).__name__, "text": str(exc)}
        return MediationOutcome(
            response=resp.failure(
                error_class=resp.ERR_PROVISIONING_UNAVAILABLE, request_label=request.request_label,
                intent_id=intent_id, backend_invocations=counting.invocations,
            ),
            m_side=m_side,
        )
    except ExecutionError as exc:
        # The Pod was created and failed, or creation was refused. The message embeds Pod logs and
        # name; R learns only the class. K-live/K-audit are the channels that are supposed to tell
        # the experiment what happened here -- see response.py's module docstring.
        m_side["exception"] = {"type": type(exc).__name__, "text": str(exc)}
        return MediationOutcome(
            response=resp.failure(
                error_class=resp.ERR_EXECUTION_FAILED, request_label=request.request_label,
                intent_id=intent_id, backend_invocations=counting.invocations,
            ),
            m_side=m_side,
        )
    except Exception as exc:  # noqa: BLE001 -- a normalized boundary is the point
        m_side["exception"] = {"type": type(exc).__name__, "text": str(exc)}
        return MediationOutcome(
            response=resp.failure(
                error_class=resp.ERR_INTERNAL, request_label=request.request_label,
                intent_id=intent_id, backend_invocations=counting.invocations,
            ),
            m_side=m_side,
        )

    m_side.update({"stage": "complete", "effect_detail": dict(effect.detail), "backend_invocations": counting.invocations})
    return MediationOutcome(
        response=resp.success(
            request_label=request.request_label,
            intent_id=intent_id,
            execution_class=effect.execution_class,
            backend_invocations=counting.invocations,
            effect_detail=effect.detail,
        ),
        m_side=m_side,
    )


def _default_backend(config: MediatorConfig) -> ExecutionBackend:
    """The real, unmodified `K8sPodBackend`. Its Kubernetes credential is ambient to THIS process
    (execution_k8s.py:121-128 shells out to kubectl), which is exactly why running this process as
    M rather than as R is the whole enforcement mechanism -- see README.md, Finding 1."""
    return K8sPodBackend(
        namespace=config.namespace,
        image=config.image,
        kubectl=config.kubectl,
        context=config.kubectl_context,
        timeout=config.timeout_seconds,
    )
