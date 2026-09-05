"""The artifact_code R submits through the mediator. This is data, and it runs INSIDE THE POD.

Pre-registration mapping: the positive case's marker artifact (criterion 4) and falsification case
F-05 (in-cluster ServiceAccount token).

Under the registered threat model R may supply arbitrary code here -- that is a legitimate
capability of R, not a vulnerability. What must never happen is this text becoming host-shell
syntax on M's side. It does not: `K8sPodBackend` embeds it in a JSON Pod manifest and pipes that to
`kubectl apply -f -` using a list argv with no shell (execution_k8s.py:127-128, 154, 159), so shell
metacharacters here are inert on the host and merely ordinary characters inside the Pod's Python.

F-05 SECRET DISCIPLINE, ENFORCED IN THE ARTIFACT ITSELF. The token probe never prints the token.
It prints presence, readability, length, a truncated SHA-256 fingerprint, whitelisted JWT claims
(`iss`/`sub`/`aud`/`exp` -- identity, not authentication material), and the API server's own
SelfSubjectAccessReview answer. The raw token and the JWT signature never enter stdout, and
therefore never enter the Pod logs, the mediated response, the evidence directory or Git.

The probe runs on `python:3.12-slim` with the standard library only -- no `requests`, no
`kubernetes` client, nothing to install.
"""
from __future__ import annotations

RESULT_SENTINEL = "SIPHO-BYPASS-RESULT:"


def marker_artifact(marker: str) -> str:
    """Positive-case artifact. Emits a unique marker so the mediated execution can be correlated
    with the independently observed Pod without reading anything off `Effect`."""
    if not marker.replace("-", "").isalnum():
        raise ValueError("marker must be alphanumeric with dashes")
    return (
        "import json\n"
        f"print({RESULT_SENTINEL!r} + json.dumps({{'marker': {marker!r}, 'ok': True}}))\n"
    )


# Built as a plain string rather than an f-string: the body contains many braces, and this keeps
# what runs in the Pod byte-for-byte readable here.
SA_TOKEN_PROBE = '''
import base64, hashlib, json, os, ssl, urllib.request, urllib.error

SENTINEL = "''' + RESULT_SENTINEL + '''"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = os.path.join(SA_DIR, "token")
CA_PATH = os.path.join(SA_DIR, "ca.crt")
NS_PATH = os.path.join(SA_DIR, "namespace")

out = {
    "probe": "f05_serviceaccount_token",
    "sa_dir_present": os.path.isdir(SA_DIR),
    "token_present": os.path.exists(TOKEN_PATH),
    "token_readable": None,
    "token_length": None,
    "token_fingerprint": None,
    "token_identity": None,
    "token_claims": None,
    "token_error": None,
    "namespace": None,
    "ssar_attempted": False,
    "ssar_allowed": None,
    "ssar_error": None,
}

token = None
if out["token_present"]:
    try:
        with open(TOKEN_PATH, "r") as fh:
            token = fh.read().strip()
        out["token_readable"] = True
        out["token_length"] = len(token)
        # Truncated SHA-256 only. The token itself is never emitted.
        out["token_fingerprint"] = hashlib.sha256(token.encode()).hexdigest()[:16]
    except OSError as exc:
        out["token_readable"] = False
        out["token_error"] = "errno=%s" % getattr(exc, "errno", None)

if token:
    # Decode ONLY the claims segment, and only a whitelist of claims. The signature segment is
    # never touched. `sub` is an identity string such as
    # "system:serviceaccount:default:default" -- identity, not a secret.
    try:
        parts = token.split(".")
        if len(parts) == 3:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            keep = {}
            for key in ("iss", "sub", "aud", "exp"):
                if key in claims:
                    keep[key] = claims[key]
            k8s = claims.get("kubernetes.io") or {}
            if isinstance(k8s, dict):
                if "namespace" in k8s:
                    keep["k8s_namespace"] = k8s["namespace"]
                sa = k8s.get("serviceaccount")
                if isinstance(sa, dict) and "name" in sa:
                    keep["k8s_serviceaccount"] = sa["name"]
            out["token_claims"] = keep
            out["token_identity"] = keep.get("sub") or keep.get("k8s_serviceaccount")
    except Exception as exc:
        out["token_error"] = "claim_decode:%s" % type(exc).__name__

try:
    with open(NS_PATH) as fh:
        out["namespace"] = fh.read().strip()
except OSError:
    pass

# SelfSubjectAccessReview: ask the API server whether THIS identity may create pods. A read-only
# authorization query -- it mutates nothing, so it is safe to run unconditionally and it answers
# F-05's real question without creating anything.
if token and out["namespace"]:
    body = json.dumps({
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": {
            "namespace": out["namespace"], "verb": "create", "group": "", "resource": "pods",
        }},
    }).encode()
    url = "https://kubernetes.default.svc/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    try:
        ctx = ssl.create_default_context(cafile=CA_PATH) if os.path.exists(CA_PATH) else ssl.create_default_context()
        out["ssar_attempted"] = True
        with urllib.request.urlopen(req, timeout=15, context=ctx) as handle:
            answer = json.loads(handle.read(65536).decode("utf-8"))
        status = answer.get("status") or {}
        out["ssar_allowed"] = bool(status.get("allowed"))
    except urllib.error.HTTPError as exc:
        out["ssar_error"] = "http:%s" % exc.code
    except Exception as exc:
        out["ssar_error"] = "transport:%s" % type(exc).__name__

print(SENTINEL + json.dumps(out, sort_keys=True))
'''


def parse_sentinel(stdout: str | None) -> dict | None:
    """Pull the probe's single JSON line out of the mediated response's bounded stdout."""
    if not stdout:
        return None
    for line in stdout.splitlines():
        if line.startswith(RESULT_SENTINEL):
            try:
                import json

                parsed = json.loads(line[len(RESULT_SENTINEL):])
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None
