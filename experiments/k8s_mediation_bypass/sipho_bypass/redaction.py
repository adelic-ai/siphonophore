"""Secret hygiene for everything this experiment serializes.

Pre-registration mapping: criterion 7, falsification case F-10, and the evidence-directory rule
"credential PRESENCE/FINGERPRINT metadata only where secrets are involved; no raw persistent
credential".

Two independent mechanisms, because either alone fails in a predictable way:

1. A key-name denylist catches a field someone NAMED honestly (`token`, `kubeconfig`).
2. A value-shape scan catches a secret that arrived under an innocent name -- a JWT pasted into
   `stdout`, a PEM private key inside a captured stderr.

`assert_no_secrets` RAISES rather than silently redacting. Silent redaction would let a leak be
introduced and never noticed; a raised exception fails the run that produced it, which is the
behavior an experiment about credential custody should have.

Public keys and certificates are deliberately NOT treated as secrets. The cluster CA certificate is
a trust anchor R is expected to hold under the implementation clarification recorded in README.md;
flagging it would make the direct-API probe unable to record its own configuration.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

FINGERPRINT_LEN = 16


class SecretLeakError(RuntimeError):
    """A structure about to be serialized contained something that looks like a credential."""

    def __init__(self, findings: list[str]) -> None:
        super().__init__("refusing to serialize: possible credential material at " + ", ".join(findings))
        self.findings = findings


_SECRET_KEY_SUBSTRINGS = (
    "token", "secret", "password", "passwd", "kubeconfig", "bearer",
    "client_key", "client-key", "private_key", "private-key", "credential",
    "authorization", "apikey", "api_key",
)

# A key that CONTAINS a denylisted substring but is known to carry only derived, non-reversible or
# boolean metadata. Kept explicit and small so adding one is a visible decision.
_SECRET_KEY_ALLOWLIST = frozenset({
    "token_present", "token_readable", "token_fingerprint", "token_length",
    "token_identity", "token_path", "token_error",
    "kubeconfig_path", "kubeconfig_readable", "kubeconfig_present", "kubeconfig_env",
    "kubeconfig_mode", "kubeconfig_owner_uid", "kubeconfig_read_errno",
    "credential_files", "credential_paths_searched", "credential_paths_not_searched",
    "secret_scan_findings", "kubeconfig_env_present", "sa_token_dir",
})

_JWT_RE = re.compile(r"\bey[A-Za-z0-9_-]{8,}\.ey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM_PRIVATE_RE = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
# A kubeconfig's embedded client credential, base64 under a well-known key.
_KUBECONFIG_CLIENT_KEY_RE = re.compile(r"client-key-data\s*:")


def fingerprint(value: str | bytes) -> str:
    """A truncated SHA-256 of a credential, for correlating "the same token" across artifacts
    without ever storing it. Not reversible for a high-entropy input such as a JWT; this is not
    offered as a general-purpose anonymizer for low-entropy values."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()[:FINGERPRINT_LEN]


def _is_path_shaped(key: str) -> bool:
    """A filesystem path used as a MAP KEY is data in key position, not a field name.

    The key denylist exists to catch a field someone named honestly (`bearer_token`). It must not
    fire on `credential_files["/var/run/secrets/kubernetes.io/serviceaccount/token"]`, whose key is
    the very path the experiment is required to report on. Values under such keys are still
    scanned normally, so a real credential appearing there is still caught by shape."""
    return key.startswith("/") or key.startswith("~") or "\\" in key


def _scan(obj: Any, path: str, findings: list[str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_str = str(key)
            here = f"{path}.{key_str}" if path else key_str
            lowered = key_str.lower()
            if (
                not _is_path_shaped(key_str)
                and key_str not in _SECRET_KEY_ALLOWLIST
                and any(s in lowered for s in _SECRET_KEY_SUBSTRINGS)
            ):
                findings.append(f"{here} (denylisted key name)")
            _scan(val, here, findings)
    elif isinstance(obj, (list, tuple)):
        for i, val in enumerate(obj):
            _scan(val, f"{path}[{i}]", findings)
    elif isinstance(obj, str):
        if _JWT_RE.search(obj):
            findings.append(f"{path} (JWT-shaped value)")
        if _PEM_PRIVATE_RE.search(obj):
            findings.append(f"{path} (PEM private key)")
        if _KUBECONFIG_CLIENT_KEY_RE.search(obj):
            findings.append(f"{path} (kubeconfig client-key-data)")


def find_secrets(obj: Any) -> list[str]:
    findings: list[str] = []
    _scan(obj, "", findings)
    return findings


def assert_no_secrets(obj: Any) -> None:
    findings = find_secrets(obj)
    if findings:
        raise SecretLeakError(findings)


def safe_json_dumps(obj: Any, **kwargs: Any) -> str:
    """`json.dumps` that refuses to emit credential-shaped content."""
    assert_no_secrets(obj)
    kwargs.setdefault("indent", 2)
    kwargs.setdefault("sort_keys", True)
    return json.dumps(obj, allow_nan=False, **kwargs)
