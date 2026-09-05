"""Adversarial tests for the mediator request protocol -- the R -> M boundary.

Pre-registration mapping: bypass case C1 ("R attempts to submit a forged/altered Decision to M --
structurally impossible, the boundary accepts only an Intent") and falsification case F-12 (helper
argument/stdin injection).

Every test here models R as hostile. `artifact_code` is NOT modelled as hostile input to the
protocol: R is entitled to supply arbitrary code for execution in the Pod, and the tests assert
that it passes through intact rather than being sanitized.
"""
from __future__ import annotations

import json

import pytest

from sipho_bypass import protocol


def _ok(**overrides):
    body = {"protocol_version": protocol.PROTOCOL_VERSION, "kind": "run_artifact",
            "artifact_code": "print('hi')"}
    body.update(overrides)
    return json.dumps(body).encode()


def test_minimal_valid_request_round_trips():
    req = protocol.parse_request(protocol.build_request("run_artifact", "print('hi')"))
    assert (req.kind, req.artifact_code, req.payload, req.request_label) == (
        "run_artifact", "print('hi')", {}, None)


# --- C1: a Decision has no slot at this boundary -----------------------------------------------

@pytest.mark.parametrize("field_name", sorted(protocol.FORBIDDEN_FIELDS))
def test_every_mediator_owned_field_is_refused_distinctly(field_name):
    """C1 generalized. Each of these names something M alone decides; a distinct error code makes
    'R tried to choose this' visible in the evidence record rather than looking like a typo."""
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(**{field_name: "anything"}))
    assert exc.value.code == "forbidden_field"
    assert field_name in exc.value.detail["fields"]


def test_decision_and_token_specifically_refused():
    for name in ("decision", "token", "gate_secret", "authority"):
        with pytest.raises(protocol.ProtocolError) as exc:
            protocol.parse_request(_ok(**{name: {"permitted": True}}))
        assert exc.value.code == "forbidden_field"


def test_consequence_cannot_be_chosen():
    """The single most dangerous field. ConsequencePolicy maps an unknown consequence to
    `same_process` (policy.py:86), which would execute R's artifact_code inside M's process."""
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(consequence="low"))
    assert exc.value.code == "forbidden_field"


def test_execution_class_cannot_be_chosen():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(execution_class="same_process"))
    assert exc.value.code == "forbidden_field"


@pytest.mark.parametrize("name", ["kubeconfig", "context", "namespace", "kubectl", "image", "env", "output_path"])
def test_substrate_and_environment_fields_cannot_be_chosen(name):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(**{name: "/tmp/evil"}))
    assert exc.value.code == "forbidden_field"


# --- schema discipline --------------------------------------------------------------------------

def test_unknown_field_rejected_and_sample_is_bounded():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(**{"surprise": 1, "another": 2}))
    assert exc.value.code == "unknown_field"
    assert exc.value.detail["count"] == 2
    assert all(len(s) <= 32 for s in exc.value.detail["sample"])


def test_unknown_field_name_is_not_echoed_verbatim_when_unprintable():
    payload = json.dumps({"protocol_version": 1, "kind": "run_artifact",
                          "artifact_code": "x", "\x07bell": 1}).encode()
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(payload)
    assert exc.value.code == "unknown_field"
    assert exc.value.detail["sample"] == []


@pytest.mark.parametrize("missing", ["protocol_version", "kind", "artifact_code"])
def test_missing_required_field_rejected(missing):
    body = json.loads(_ok())
    del body[missing]
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(json.dumps(body).encode())
    assert exc.value.code == "missing_field"


def test_duplicate_keys_rejected():
    """`json.loads` silently keeps the last duplicate, which would make the wire format ambiguous
    and let a rejected value be smuggled past a reader that saw only the first."""
    raw = b'{"protocol_version":1,"kind":"run_artifact","artifact_code":"a","artifact_code":"b"}'
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(raw)
    assert exc.value.code == "duplicate_field"


def test_duplicate_forbidden_key_does_not_leak_arbitrary_name():
    raw = b'{"protocol_version":1,"kind":"run_artifact","artifact_code":"a","zzz":1,"zzz":2}'
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(raw)
    assert exc.value.detail["field"] == "<redacted>"


def test_malformed_json_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(b"{not json")
    assert exc.value.code == "malformed_json"


@pytest.mark.parametrize("raw,code", [
    (b"", "empty_request"),
    (b"[]", "not_an_object"),
    (b'"a string"', "not_an_object"),
    (b"\xff\xfe", "not_utf8"),
])
def test_shape_rejections(raw, code):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(raw)
    assert exc.value.code == code


def test_oversized_request_rejected_before_parsing():
    raw = b"{" + b"x" * (protocol.MAX_REQUEST_BYTES + 1)
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(raw)
    assert exc.value.code == "request_too_large"


def test_oversized_artifact_code_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(artifact_code="x" * (protocol.MAX_ARTIFACT_CODE_BYTES + 1)))
    assert exc.value.code == "artifact_code_too_large"


def test_oversized_payload_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(payload={"k": "x" * (protocol.MAX_PAYLOAD_BYTES + 1)}))
    assert exc.value.code == "payload_too_large"


def test_wrong_protocol_version_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(protocol_version=999))
    assert exc.value.code == "unsupported_protocol_version"


def test_bool_is_not_an_acceptable_protocol_version():
    """`isinstance(True, int)` is True in Python; the check must exclude bool explicitly."""
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(protocol_version=True))
    assert exc.value.code == "bad_protocol_version_type"


def test_unexpressible_kind_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(kind="delete_everything"))
    assert exc.value.code == "kind_not_expressible"


def test_expressible_but_unauthorized_kind_is_accepted_by_the_protocol():
    """The protocol must not do the Gate's job. `write_file` is a real intent kind that M's
    Authority scope does not permit; the refusal belongs to the real mediation path, where it is
    observable, not to input validation, where it would be invisible."""
    req = protocol.parse_request(_ok(kind="write_file"))
    assert req.kind == "write_file"


def test_nul_byte_in_artifact_code_rejected():
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(artifact_code="print('a')\x00"))
    assert exc.value.code == "nul_byte_in_field"


@pytest.mark.parametrize("label", ["../../etc/passwd", "Has-Capitals", "-leading", "a" * 40, "sp ace", "a;b"])
def test_bad_request_label_rejected(label):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(request_label=label))
    assert exc.value.code == "bad_request_label_format"


def test_good_request_label_accepted():
    assert protocol.parse_request(_ok(request_label="attempt-01")).request_label == "attempt-01"


@pytest.mark.parametrize("payload", [[], "str", 3, None])
def test_bad_payload_type_rejected(payload):
    with pytest.raises(protocol.ProtocolError) as exc:
        protocol.parse_request(_ok(payload=payload))
    assert exc.value.code == "bad_payload_type"


def test_nan_payload_rejected():
    """`json.dumps` accepts NaN by default and emits invalid JSON; the manifest downstream must
    never carry it."""
    raw = b'{"protocol_version":1,"kind":"run_artifact","artifact_code":"x","payload":{"n":NaN}}'
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_request(raw)


# --- artifact_code is DATA, and must survive intact ----------------------------------------------

@pytest.mark.parametrize("code", [
    "import os; os.system('id')",
    "print('$(whoami)')",
    "print('`id`')",
    "print('a; rm -rf /')",
    "print('\"quoted\" and \\'single\\'')",
    "x = {'nested': {'braces': 1}}\nprint(x)",
    "print('--kubeconfig=/etc/evil')",
    "open('/var/run/secrets/kubernetes.io/serviceaccount/token').read()",
])
def test_hostile_looking_artifact_code_passes_through_unmodified(code):
    """R may supply arbitrary code: it runs in the Pod, which is the point. The protocol must not
    sanitize it -- sanitizing would silently change what the experiment executes. Host-shell safety
    comes from K8sPodBackend using a list argv with no shell, not from filtering here."""
    assert protocol.parse_request(_ok(artifact_code=code)).artifact_code == code


def test_artifact_code_with_format_braces_survives_backend_wrapper():
    """`K8sPodBackend` does `_ARTIFACT_WRAPPER.format(body=artifact_code)` (execution_k8s.py:137).
    Braces in the SUBSTITUTED VALUE are not re-processed by str.format -- verified here rather than
    assumed, because a regression would corrupt R's code silently."""
    code = "d = {'k': 'v'}\nprint('{not_a_field}')"
    wrapper = "\nimport json, sys\npayload = json.loads(sys.argv[1])\n{body}\n"
    assert code in wrapper.format(body=protocol.parse_request(_ok(artifact_code=code)).artifact_code)
