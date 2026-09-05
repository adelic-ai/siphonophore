"""The mediator request/response protocol -- the ONLY thing that crosses the R -> M boundary.

Pre-registration mapping: implements the boundary described under "Proposed authority topology"
("The mediator accepts an `Intent` (and optionally an `Authority`). It never accepts a
`Decision`."), and makes bypass case C1 structurally impossible rather than merely rejected.

Design rules, all enforced here rather than documented and hoped for:

- Fixed schema. Unknown fields are rejected, not ignored.
- A field the requester must NOT be able to influence is rejected with a DISTINCT error code
  (`forbidden_field`) rather than folded into `unknown_field`, so an injection test can assert
  which rule fired and so the evidence record shows the boundary was deliberate.
- Hard byte bound applied to the raw stream BEFORE parsing.
- Duplicate JSON keys are rejected (`json.loads` silently keeps the last one, which would make
  the wire format ambiguous and the evidence record unreproducible).
- No pickle, no eval, no caller-selected module, no caller-supplied filesystem path, no caller
  supplied command fragment anywhere in this file or downstream of it.
- One request per invocation.

`artifact_code` is deliberately attacker-controlled: under the registered threat model R may supply
arbitrary code to be executed IN THE TARGET POD. That is a legitimate capability of R, not a
vulnerability. It is data handed to Siphonophore, and it must never become host-shell syntax on
M's side -- `K8sPodBackend` passes it through a JSON manifest to `kubectl apply -f -` using a list
argv with no shell (execution_k8s.py:127-128, 159), which is what keeps those two facts separate.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# --- bounds -----------------------------------------------------------------------------------
MAX_REQUEST_BYTES = 64 * 1024
MAX_ARTIFACT_CODE_BYTES = 16 * 1024
MAX_PAYLOAD_BYTES = 4 * 1024
MAX_LABEL_LEN = 32

# Response stdout bound. Deliberately generous (8 KiB): a Kubernetes ServiceAccount JWT is roughly
# 1 KiB, and falsification case F-05 asks whether R could exfiltrate one through the Pod-log
# channel. Truncating below that length would make F-05 pass for the wrong reason -- the experiment
# would be measuring its own output limit rather than the deployment. See README.md F-05.
MAX_STDOUT_BYTES = 8 * 1024

# --- schema -----------------------------------------------------------------------------------
REQUIRED_FIELDS = frozenset({"protocol_version", "kind", "artifact_code"})
OPTIONAL_FIELDS = frozenset({"payload", "request_label"})
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS

# Fields that name something the mediator alone decides. Rejected distinctly from a mere typo so
# that "R tried to choose this" is visible in the evidence record. `consequence` and
# `execution_class` are the two that matter most: ConsequencePolicy.evaluate() maps an UNKNOWN
# consequence to "same_process" (policy.py:86), so a requester able to set `consequence` could
# select in-process execution of its own artifact_code inside the mediator process -- i.e. arbitrary
# code execution as M, and therefore M's Kubernetes credential. This list is the first of two
# independent defenses against that; the second is that the mediator registers ONLY the k8s_pod
# backend (see mediator/service.py).
FORBIDDEN_FIELDS = frozenset({
    "decision", "token", "decision_token", "gate_secret", "authority", "order",
    "execution_class", "consequence", "principal_id", "intent_id",
    "kubeconfig", "kubectl", "context", "namespace", "image", "timeout",
    "env", "environ", "cwd", "home", "path", "output_path", "evidence_dir",
})

# Kinds the protocol can EXPRESS. This is deliberately wider than what the mediator's Authority
# scope permits: the protocol must not do the Gate's job. A `kind` outside M's authorized set is
# accepted by the protocol and then refused by the real mediation path, which is the behavior the
# experiment wants to observe. Both values are real Siphonophore intent kinds
# (policy.py:DEFAULT_ALLOWED_KINDS).
EXPRESSIBLE_KINDS = ("run_artifact", "write_file")

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,%d}$" % (MAX_LABEL_LEN - 1))


class ProtocolError(ValueError):
    """A request was rejected by the boundary before any Siphonophore object was constructed.

    `code` is a stable, enumerated string -- the evidence record and the injection tests assert on
    it. `detail` is built from this module's own constants only; it never echoes attacker-supplied
    content back except for a field NAME already known to be in a bounded set."""

    def __init__(self, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class MediationRequest:
    """A validated request. Everything the mediator needs from R, and nothing else."""

    protocol_version: int
    kind: str
    artifact_code: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_label: str | None = None


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ProtocolError("duplicate_field", {"field": key if key in ALLOWED_FIELDS | FORBIDDEN_FIELDS else "<redacted>"})
        seen.add(key)
    return dict(pairs)


def _reject_control_chars(name: str, value: str, *, allow_newlines: bool) -> None:
    if "\x00" in value:
        raise ProtocolError("nul_byte_in_field", {"field": name})
    if not allow_newlines and ("\n" in value or "\r" in value):
        raise ProtocolError("newline_in_field", {"field": name})


def parse_request(raw: bytes) -> MediationRequest:
    """Validate and decode one request. Raises ProtocolError; never raises anything else for
    attacker-controlled input, and never partially constructs a Siphonophore object."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ProtocolError("not_bytes")
    if len(raw) == 0:
        raise ProtocolError("empty_request")
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError("request_too_large", {"limit_bytes": MAX_REQUEST_BYTES, "actual_bytes": len(raw)})

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("not_utf8") from None

    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ProtocolError:
        raise
    except json.JSONDecodeError:
        raise ProtocolError("malformed_json") from None

    if not isinstance(obj, dict):
        raise ProtocolError("not_an_object", {"top_level_type": type(obj).__name__})

    keys = set(obj)
    forbidden = sorted(keys & FORBIDDEN_FIELDS)
    if forbidden:
        raise ProtocolError("forbidden_field", {"fields": forbidden})
    unknown = sorted(keys - ALLOWED_FIELDS)
    if unknown:
        # Names are attacker-supplied; report only the count and a bounded, sanitized sample so an
        # arbitrary string never reaches a log verbatim.
        sample = [k[:32] for k in unknown[:5] if k.isprintable() and "\x00" not in k]
        raise ProtocolError("unknown_field", {"count": len(unknown), "sample": sample})
    missing = sorted(REQUIRED_FIELDS - keys)
    if missing:
        raise ProtocolError("missing_field", {"fields": missing})

    version = obj["protocol_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProtocolError("bad_protocol_version_type")
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_protocol_version", {"supported": PROTOCOL_VERSION, "requested": version})

    kind = obj["kind"]
    if not isinstance(kind, str):
        raise ProtocolError("bad_kind_type")
    if kind not in EXPRESSIBLE_KINDS:
        raise ProtocolError("kind_not_expressible", {"expressible": list(EXPRESSIBLE_KINDS)})

    artifact_code = obj["artifact_code"]
    if not isinstance(artifact_code, str):
        raise ProtocolError("bad_artifact_code_type")
    encoded = artifact_code.encode("utf-8")
    if len(encoded) == 0:
        raise ProtocolError("empty_artifact_code")
    if len(encoded) > MAX_ARTIFACT_CODE_BYTES:
        raise ProtocolError("artifact_code_too_large", {"limit_bytes": MAX_ARTIFACT_CODE_BYTES, "actual_bytes": len(encoded)})
    # Newlines are expected (it is Python source). A NUL is not, and would be a fixture bug or an
    # attempt at a truncation trick somewhere downstream.
    _reject_control_chars("artifact_code", artifact_code, allow_newlines=True)

    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("bad_payload_type")
    try:
        payload_encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ProtocolError("payload_not_json_serializable") from None
    if len(payload_encoded) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("payload_too_large", {"limit_bytes": MAX_PAYLOAD_BYTES, "actual_bytes": len(payload_encoded)})
    for key in payload:
        if not isinstance(key, str):
            raise ProtocolError("bad_payload_key_type")

    label = obj.get("request_label")
    if label is not None:
        if not isinstance(label, str):
            raise ProtocolError("bad_request_label_type")
        if not _LABEL_RE.match(label):
            # Charset is deliberately narrower than a Kubernetes label value needs. The mediator
            # embeds this in intent_id, which K8sPodBackend.pod_name_for() then slugs -- validating
            # here means the narrowing never has to be relied on as a security property.
            raise ProtocolError("bad_request_label_format", {"pattern": _LABEL_RE.pattern})

    return MediationRequest(
        protocol_version=version,
        kind=kind,
        artifact_code=artifact_code,
        payload=dict(payload),
        request_label=label,
    )


def build_request(
    kind: str, artifact_code: str, payload: dict[str, Any] | None = None, request_label: str | None = None,
) -> bytes:
    """Requester-side helper. Produces exactly what `parse_request` accepts, so R never hand-rolls
    the wire format and the injection tests have an unambiguous baseline to mutate."""
    body: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": kind,
        "artifact_code": artifact_code,
    }
    if payload is not None:
        body["payload"] = payload
    if request_label is not None:
        body["request_label"] = request_label
    return json.dumps(body, sort_keys=True, allow_nan=False).encode("utf-8")
