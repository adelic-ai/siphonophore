"""Tests for the two adversarial-review findings closed in this stage.

Pre-registration mapping: falsification cases F-04 (environment inheritance into the mediator) and
F-12 (influencing the kubectl invocation), plus the CRITICAL TRUST RULE about R-writable imports.

Both findings are consequences of README.md Finding 1 -- the backend's substrate authority is
ambient to its process -- so both are fixed by making the ambient inputs explicit, mediator-side,
with no Siphonophore change.
"""
from __future__ import annotations

import pytest

from sipho_bypass.mediator import hardening
from sipho_bypass.mediator.__main__ import EXIT_UNSAFE_DEPLOYMENT, main
from sipho_bypass.mediator.config import MediatorConfig


def _hardened_cfg(**kw):
    base = dict(kubectl="/usr/bin/kubectl", kubeconfig="/etc/sipho/m.kubeconfig",
                enforce_deployment_hardening=True)
    base.update(kw)
    return MediatorConfig(**base)


# --- H-1: the kubectl binary must not be PATH-resolved ---------------------------------------------

def test_bare_kubectl_name_is_refused_in_a_hardened_deployment():
    findings = hardening.deployment_findings(_hardened_cfg(kubectl="kubectl"), sys_path=[], environ={})
    assert any("absolute path" in f for f in findings)


def test_absolute_kubectl_is_accepted():
    assert hardening.deployment_findings(_hardened_cfg(), sys_path=[], environ={}) == []


def test_relative_kubectl_path_is_refused():
    findings = hardening.deployment_findings(_hardened_cfg(kubectl="./kubectl"), sys_path=[], environ={})
    assert any("absolute path" in f for f in findings)


# --- H-2: the credential path must not depend on HOME ------------------------------------------------

def test_missing_kubeconfig_is_refused():
    findings = hardening.deployment_findings(_hardened_cfg(kubeconfig=None), sys_path=[], environ={})
    assert any("kubeconfig must be configured" in f for f in findings)


def test_kubeconfig_is_set_explicitly_and_not_inherited():
    env = {"KUBECONFIG": "/home/requester/evil.kubeconfig", "HOME": "/home/requester"}
    report = hardening.harden_environment(_hardened_cfg(mediator_home="/var/lib/sipho-mediator"), env)
    assert env["KUBECONFIG"] == "/etc/sipho/m.kubeconfig"
    assert env["HOME"] == "/var/lib/sipho-mediator"
    assert "KUBECONFIG" in report.scrubbed


def test_requester_kubeconfig_is_scrubbed_even_when_the_mediator_configures_none():
    env = {"KUBECONFIG": "/home/requester/evil.kubeconfig"}
    hardening.harden_environment(MediatorConfig(), env)
    assert "KUBECONFIG" not in env


# --- interpreter/loader scrubbing ---------------------------------------------------------------------

@pytest.mark.parametrize("name", ["PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
                                  "BASH_ENV", "IFS", "KUBERNETES_MASTER"])
def test_dangerous_environment_variables_are_removed(name):
    env = {name: "/home/requester/evil"}
    report = hardening.harden_environment(MediatorConfig(), env)
    assert name not in env
    assert name in report.scrubbed


def test_path_is_replaced_with_a_fixed_safe_value():
    env = {"PATH": "/home/requester/bin:/usr/bin"}
    hardening.harden_environment(MediatorConfig(), env)
    assert env["PATH"] == hardening.DEFAULT_SAFE_PATH
    assert "/home/requester/bin" not in env["PATH"]


def test_safe_path_is_configurable_by_the_mediator_only():
    env = {}
    hardening.harden_environment(MediatorConfig(safe_path="/opt/sipho/bin:/usr/bin"), env)
    assert env["PATH"] == "/opt/sipho/bin:/usr/bin"


# --- import isolation -----------------------------------------------------------------------------

def test_empty_sys_path_entry_is_a_deployment_finding():
    findings = hardening.deployment_findings(_hardened_cfg(), sys_path=["", "/opt/x"], environ={})
    assert any("-I" in f for f in findings)


def test_pythonpath_present_is_a_deployment_finding():
    findings = hardening.deployment_findings(_hardened_cfg(), sys_path=[],
                                             environ={"PYTHONPATH": "/home/requester"})
    assert any("PYTHONPATH" in f for f in findings)


# --- the entry point actually enforces it -------------------------------------------------------------

def test_entry_point_refuses_to_start_on_an_unsafe_deployment(tmp_path, capsys):
    import json
    cfg = tmp_path / "mediator.json"
    cfg.write_text(json.dumps({"kubectl": "kubectl", "enforce_deployment_hardening": True}))
    rc = main(argv=[], stdin_bytes=b"{}", config_path=str(cfg))
    assert rc == EXIT_UNSAFE_DEPLOYMENT
    err = capsys.readouterr().err
    assert "deployment hardening preconditions unmet" in err
    assert "absolute path" in err


def test_a_misconfigured_fixture_never_produces_a_response(tmp_path, capsys):
    """INCONCLUSIVE, never a quietly weaker run: no response line is emitted at all."""
    import json
    cfg = tmp_path / "mediator.json"
    cfg.write_text(json.dumps({"kubectl": "kubectl", "enforce_deployment_hardening": True}))
    main(argv=[], stdin_bytes=b"{}", config_path=str(cfg))
    assert capsys.readouterr().out == ""


def test_unit_tests_are_not_subject_to_deployment_hardening_by_default():
    """These are properties of a provisioned host; asserting them in a test venv would only
    measure the test venv."""
    assert MediatorConfig().enforce_deployment_hardening is False
