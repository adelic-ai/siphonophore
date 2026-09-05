"""The mediator's own behavior at the boundary -- no cluster, no credential, no privilege.

Pre-registration mapping: the positive case's construction (criterion 4), response redaction
(criterion 7, F-10), and the barriers that stop R selecting in-process execution.

A fake backend stands in for `K8sPodBackend` throughout. That substitution is available only to
this test module -- it is not expressible in the wire protocol and `__main__` never passes it.
"""
from __future__ import annotations

import json

import pytest

from siphonophore_core.execution import ExecutionBackend, ExecutionError
from siphonophore_core.execution_k8s import ProvisioningError
from siphonophore_core.intent import Effect

from sipho_bypass import protocol, response as resp
from sipho_bypass.mediator import service
from sipho_bypass.mediator.config import ConfigError, MediatorConfig, load_config


class FakeBackend(ExecutionBackend):
    def __init__(self, detail=None, raises=None):
        self.detail = detail if detail is not None else {
            "pod_name": "sipho-bypass-abc-1234", "namespace": "default", "node_name": "kind-worker",
            "phase": "Succeeded", "exit_code": 0, "stdout": "hello\n",
        }
        self.raises = raises
        self.calls = []

    def run(self, decision, intent):
        self.calls.append((decision, intent))
        if self.raises is not None:
            raise self.raises
        return Effect(intent_id=intent.intent_id, execution_class="k8s_pod", detail=dict(self.detail))


def _cfg(**kw):
    return MediatorConfig(**kw)


def _handle(raw, backend, config=None):
    return service.handle_request(raw, config or _cfg(), backend_factory=lambda c: backend)


# --- the happy path -------------------------------------------------------------------------------

def test_allow_case_dispatches_through_the_real_path_exactly_once():
    backend = FakeBackend()
    out = _handle(protocol.build_request("run_artifact", "print('x')", request_label="a1"), backend)
    assert out.response.status == "ok"
    assert out.response.backend_invocations == 1          # criterion 4, counted not inferred
    assert len(backend.calls) == 1
    assert out.response.execution_class == "k8s_pod"
    assert out.response.pod_name == "sipho-bypass-abc-1234"
    assert out.response.request_label == "a1"


def test_the_decision_reaching_the_backend_was_really_minted_and_permitted():
    """The backend must be reached through Gate -> Executor, not around it."""
    backend = FakeBackend()
    _handle(protocol.build_request("run_artifact", "print('x')"), backend)
    decision, intent = backend.calls[0]
    assert decision.permitted is True
    assert decision.execution_class == "k8s_pod"
    assert decision.authority_id and decision.order_id      # a real Authority was exercised
    assert decision.intent_id == intent.intent_id


def test_mediator_chooses_principal_consequence_and_intent_id_not_the_requester():
    backend = FakeBackend()
    cfg = _cfg(requester_principal_id="bypass-requester")
    _handle(protocol.build_request("run_artifact", "print('x')", request_label="lbl"), backend, cfg)
    _decision, intent = backend.calls[0]
    assert intent.principal_id == "bypass-requester"
    assert intent.consequence == "k8s"
    assert intent.intent_id.startswith("bypass-lbl-")


def test_artifact_code_and_payload_reach_the_pod_unmodified():
    backend = FakeBackend()
    code = "import os; print('$(id)'); d={'a':1}"
    _handle(protocol.build_request("run_artifact", code, payload={"k": "v"}), backend)
    _decision, intent = backend.calls[0]
    assert intent.artifact_code == code
    assert intent.payload == {"k": "v"}


# --- the real mediation path must do the refusing --------------------------------------------------

def test_unauthorized_kind_is_refused_by_the_gate_and_the_backend_is_never_reached():
    """`write_file` is expressible by the protocol and outside M's Authority scope, so
    `Gate.submit` marks it not-permitted and `Executor` raises before any backend runs."""
    backend = FakeBackend()
    out = _handle(protocol.build_request("write_file", "print('x')"), backend)
    assert out.response.status == "error"
    assert out.response.error_class == resp.ERR_GATE_VIOLATION
    assert out.response.backend_invocations == 0
    assert backend.calls == []
    assert out.response.error_detail == {"requested_kind": "write_file",
                                         "authorized_kinds": ["run_artifact"]}


def test_only_the_k8s_pod_backend_is_registered():
    """Barrier 3. Even if execution_class were somehow anything else, no backend exists for it."""
    backend = FakeBackend()
    captured = {}

    def factory(config):
        captured["config"] = config
        return backend

    service.handle_request(protocol.build_request("run_artifact", "print('x')"), _cfg(),
                           backend_factory=factory)
    assert captured["config"].execution_class == "k8s_pod"


def test_config_refuses_a_non_k8s_execution_class():
    """Barrier 2, at construction time."""
    with pytest.raises(ConfigError):
        MediatorConfig(consequence="low")
    with pytest.raises(ConfigError):
        MediatorConfig(execution_class="same_process")


# --- error normalization / no leakage ---------------------------------------------------------------

@pytest.mark.parametrize("exc,expected", [
    (ProvisioningError("kubectl cannot reach namespace 'default': /home/mediator/.kube/config"),
     resp.ERR_PROVISIONING_UNAVAILABLE),
    (ExecutionError("k8s_pod 'sipho-x' did not succeed: phase='Failed' logs='SECRET'"),
     resp.ERR_EXECUTION_FAILED),
    (RuntimeError("some unexpected internal thing at /opt/mediator/lib"), resp.ERR_INTERNAL),
])
def test_exception_messages_never_reach_the_requester(exc, expected):
    backend = FakeBackend(raises=exc)
    out = _handle(protocol.build_request("run_artifact", "print('x')"), backend)
    assert out.response.error_class == expected
    blob = json.dumps(out.response.to_dict())
    for leak in ("/home/mediator", "SECRET", "/opt/mediator", "kubectl"):
        assert leak not in blob
    # ...while the full text IS retained M-side, where R cannot read it.
    assert str(exc) in out.m_side["exception"]["text"]


def test_protocol_rejection_is_normalized_not_raised():
    backend = FakeBackend()
    out = _handle(b'{"bad', backend)
    assert out.response.status == "error"
    assert out.response.error_class == resp.ERR_PROTOCOL
    assert out.response.error_detail["code"] == "malformed_json"
    assert backend.calls == []


def test_response_carries_only_whitelisted_effect_detail():
    """A whitelist, not a filter: `namespace` and `node_name` are present in Effect.detail and must
    NOT appear in what R receives."""
    backend = FakeBackend()
    out = _handle(protocol.build_request("run_artifact", "print('x')"), backend)
    data = out.response.to_dict()
    assert set(data) == {
        "protocol_version", "status", "request_label", "intent_id", "execution_class",
        "backend_invocations", "pod_name", "phase", "exit_code", "stdout", "stdout_truncated",
        "error_class", "error_detail",
    }
    assert "node_name" not in json.dumps(data)


def test_stdout_is_bounded_and_truncation_is_reported():
    backend = FakeBackend(detail={"pod_name": "p", "phase": "Succeeded", "exit_code": 0,
                                  "stdout": "A" * (protocol.MAX_STDOUT_BYTES + 500)})
    out = _handle(protocol.build_request("run_artifact", "print('x')"), backend)
    assert out.response.stdout_truncated is True
    assert len(out.response.stdout.encode()) <= protocol.MAX_STDOUT_BYTES


def test_stdout_bound_is_large_enough_for_f05_to_be_a_real_test():
    """A Kubernetes SA JWT is roughly 1 KiB. If the bound were below that, F-05 would 'pass'
    because of this constant rather than because of the deployment."""
    assert protocol.MAX_STDOUT_BYTES >= 4096


def test_malformed_exit_code_or_phase_is_normalized_to_none():
    backend = FakeBackend(detail={"pod_name": 42, "phase": ["weird"], "exit_code": "0", "stdout": None})
    out = _handle(protocol.build_request("run_artifact", "print('x')"), backend)
    assert (out.response.pod_name, out.response.phase, out.response.exit_code) == (None, None, None)


# --- entry point argv discipline ---------------------------------------------------------------

def test_entry_point_refuses_any_argument(tmp_path):
    from sipho_bypass.mediator import __main__ as entry
    assert entry.main(argv=["--kubeconfig=/tmp/x"], stdin_bytes=b"{}") == entry.EXIT_BAD_INVOCATION
    assert entry.main(argv=[""], stdin_bytes=b"{}") == entry.EXIT_BAD_INVOCATION


def test_entry_point_reports_missing_config_without_naming_paths(tmp_path, capsys):
    from sipho_bypass.mediator import __main__ as entry
    rc = entry.main(argv=[], stdin_bytes=b"{}", config_path=str(tmp_path / "absent.json"))
    assert rc == entry.EXIT_CONFIG
    assert "absent.json" not in capsys.readouterr().err


def test_config_rejects_unknown_keys(tmp_path):
    path = tmp_path / "mediator.json"
    path.write_text(json.dumps({"namespace": "default", "surprise": 1}))
    with pytest.raises(ConfigError):
        load_config(path)


def test_config_round_trips_known_keys(tmp_path):
    path = tmp_path / "mediator.json"
    path.write_text(json.dumps({"namespace": "bypass", "authorized_kinds": ["run_artifact"],
                                "kubectl_context": "kind-x"}))
    cfg = load_config(path)
    assert (cfg.namespace, cfg.authorized_kinds, cfg.kubectl_context) == ("bypass", ("run_artifact",), "kind-x")
    assert cfg.consequence == "k8s" and cfg.execution_class == "k8s_pod"
