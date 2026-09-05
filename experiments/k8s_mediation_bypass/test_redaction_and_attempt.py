"""Secret hygiene and evidence-directory immutability.

Pre-registration mapping: criterion 7, falsification F-10, and the evidence rule "credential
PRESENCE/FINGERPRINT metadata only where secrets are involved; no raw persistent credential".

No real credential is used anywhere in this file. The JWT-shaped strings below are structurally
valid but cryptographically meaningless -- they exist to prove the scanner fires.
"""
from __future__ import annotations

import json

import pytest

from sipho_bypass import attempt, redaction

# Structurally JWT-shaped, semantically nothing. Never a real token.
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmYWtlLW5vdC1hLXRva2VuIn0.QUJDREVGR0hJSktMTU5PUA"
FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nQUJD\n-----END RSA PRIVATE KEY-----"


def test_jwt_shaped_value_is_caught_under_an_innocent_key():
    with pytest.raises(redaction.SecretLeakError):
        redaction.assert_no_secrets({"stdout": f"here it is: {FAKE_JWT}"})


def test_pem_private_key_is_caught():
    with pytest.raises(redaction.SecretLeakError):
        redaction.assert_no_secrets({"captured_stderr": FAKE_PEM})


def test_kubeconfig_client_key_data_is_caught():
    with pytest.raises(redaction.SecretLeakError):
        redaction.assert_no_secrets({"blob": "users:\n- user:\n    client-key-data: QUJD"})


def test_denylisted_key_name_is_caught_even_with_a_harmless_value():
    with pytest.raises(redaction.SecretLeakError):
        redaction.assert_no_secrets({"bearer_token": "x"})


def test_secrets_are_found_nested_in_lists_and_dicts():
    findings = redaction.find_secrets({"a": [{"b": {"c": FAKE_JWT}}]})
    assert findings and "a[0].b.c" in findings[0]


def test_allowlisted_metadata_keys_are_permitted():
    """Presence/fingerprint metadata must be recordable, or the evidence model cannot express F-05
    at all."""
    redaction.assert_no_secrets({
        "token_present": True, "token_readable": True, "token_fingerprint": "abc123",
        "token_length": 1024, "token_identity": "system:serviceaccount:default:default",
        "kubeconfig_path": "/etc/x/m.kubeconfig", "kubeconfig_readable": False,
    })


def test_public_certificates_are_not_treated_as_secrets():
    """The cluster CA is a trust anchor R is expected to hold; flagging it would make the
    direct-API probe unable to record its own configuration."""
    redaction.assert_no_secrets({"ca_cert_path": "/etc/x/ca.crt",
                                 "pem": "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----"})


def test_fingerprint_is_stable_truncated_and_not_the_input():
    fp = redaction.fingerprint(FAKE_JWT)
    assert fp == redaction.fingerprint(FAKE_JWT)
    assert len(fp) == redaction.FINGERPRINT_LEN
    assert FAKE_JWT[:16] not in fp


def test_safe_json_dumps_refuses_rather_than_silently_redacting():
    """A silent redaction would let a leak be introduced and never noticed."""
    with pytest.raises(redaction.SecretLeakError):
        redaction.safe_json_dumps({"stdout": FAKE_JWT})
    assert json.loads(redaction.safe_json_dumps({"ok": 1})) == {"ok": 1}


def test_the_f05_artifact_never_prints_the_raw_token():
    from sipho_bypass.requester import artifacts
    source = artifacts.SA_TOKEN_PROBE
    assert '"token": token' not in source
    assert "out[\"token\"]" not in source
    # The only place `token` is turned into an output value is the fingerprint and the length.
    assert 'hashlib.sha256(token.encode()).hexdigest()[:16]' in source
    assert 'out["token_length"] = len(token)' in source


# --- attempt directory ---------------------------------------------------------------------------

def test_attempt_directory_is_created_once_and_never_reused(tmp_path):
    a = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-1")
    a.create()
    b = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-1")
    with pytest.raises(attempt.AttemptCollisionError):
        b.create()


def test_a_failed_attempt_is_never_overwritten(tmp_path):
    a = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-2")
    a.create()
    a.write_json("result", {"ok": False})
    with pytest.raises(attempt.ImmutableWriteError):
        a.write_json("result", {"ok": True})


def test_evidence_writes_run_through_the_secret_scanner(tmp_path):
    a = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-3")
    a.create()
    with pytest.raises(redaction.SecretLeakError):
        a.write_json("leaky", {"stdout": FAKE_JWT})
    assert not (a.path / "leaky.json").exists()      # nothing partially written


def test_artifact_names_cannot_escape_the_attempt_directory(tmp_path):
    a = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-4")
    a.create()
    for bad in ("../escape", "sub/dir", ".hidden"):
        with pytest.raises(ValueError):
            a.write_json(bad, {})


def test_attempt_ids_are_fresh():
    assert attempt.new_attempt_id() != attempt.new_attempt_id()


def test_written_files_are_owner_only(tmp_path):
    import stat
    a = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-5")
    a.create()
    path = a.write_json("x", {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(a.path.stat().st_mode) == 0o700


def test_provenance_records_the_exact_commit(tmp_path):
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[2]
    prov = attempt.AttemptDirectory(root=str(tmp_path)).provenance(siphonophore_repo=repo)
    assert prov["siphonophore_commit"] and len(prov["siphonophore_commit"]) == 40
    assert prov["agentwatch_commit"] is None          # not used unless a repo is supplied


def test_default_evidence_root_is_outside_the_repository():
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[2]
    assert not str(pathlib.Path(attempt.DEFAULT_EVIDENCE_ROOT)).startswith(str(repo))


def test_round_trip_load(tmp_path):
    a = attempt.AttemptDirectory(root=str(tmp_path), attempt_id="fixed-6")
    a.create()
    a.write_json("one", {"a": 1})
    a.write_json("two", {"b": 2})
    loaded = attempt.load_attempt(a.path)
    assert loaded["one.json"] == {"a": 1} and loaded["two.json"] == {"b": 2}
    assert a.manifest()["artifacts"] == ["one.json", "two.json"]


def test_path_shaped_map_keys_are_not_mistaken_for_field_names():
    """`credential_files` is keyed by path, and the experiment is REQUIRED to report on
    /var/run/secrets/kubernetes.io/serviceaccount/token. Flagging the key would make the snapshot
    unserializable; flagging a real token in the VALUE must still work."""
    redaction.assert_no_secrets({
        "credential_files": {"/var/run/secrets/kubernetes.io/serviceaccount/token": {"readable": False}},
    })
    with pytest.raises(redaction.SecretLeakError):
        redaction.assert_no_secrets({
            "credential_files": {"/var/run/secrets/kubernetes.io/serviceaccount/token": FAKE_JWT},
        })


def test_an_honestly_named_secret_field_is_still_caught():
    with pytest.raises(redaction.SecretLeakError):
        redaction.assert_no_secrets({"service_account_token": "anything"})
