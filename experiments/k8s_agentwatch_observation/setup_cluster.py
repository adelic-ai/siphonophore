"""Creates the SECOND, audit-configured kind cluster this experiment needs -- deliberately
separate from `kind-siphonophore-demo` (the cluster the committed K8sPodBackend vertical slice
uses), since kind bakes audit-policy wiring into the API server at kubeadm-bootstrap time; adding
it to an already-running cluster would need unsupported static-control-plane-manifest surgery
(confirmed, not attempted). K8sPodBackend needs no code change to target this cluster instead --
its existing `context=` constructor parameter does the whole job (see test_stage1_audit_observation.py).

Idempotent: does nothing if the cluster already exists.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CLUSTER_NAME = "sipho-agentwatch-audit"
KUBE_CONTEXT = f"kind-{CLUSTER_NAME}"

HERE = Path(__file__).resolve().parent
AUDIT_POLICY_PATH = HERE / "kind" / "audit-policy.yaml"
AUDIT_LOG_DIR = HERE / "kind" / "audit-logs"  # gitignored -- runtime output, not source
RENDERED_CONFIG_PATH = HERE / "kind" / ".rendered-kind-config.yaml"  # gitignored -- generated
TEMPLATE_PATH = HERE / "kind" / "kind-config.yaml.tmpl"


def _existing_clusters() -> set[str]:
    result = subprocess.run(["kind", "get", "clusters"], capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def render_config() -> Path:
    template = TEMPLATE_PATH.read_text()
    rendered = template.format(AUDIT_POLICY_PATH=str(AUDIT_POLICY_PATH), AUDIT_LOG_DIR=str(AUDIT_LOG_DIR))
    RENDERED_CONFIG_PATH.write_text(rendered)
    return RENDERED_CONFIG_PATH


def fix_audit_log_permissions() -> None:
    """The API server writes audit.log as root:root 0600 inside the node container -- unreadable
    from the host bind mount otherwise. Same fix AgentWatch's own demo/k8s/README.md documents as a
    required manual step for the identical setup."""
    node = f"{CLUSTER_NAME}-control-plane"
    # Wait for the file to exist -- the API server may not have written its first line yet.
    for _ in range(30):
        check = subprocess.run(["docker", "exec", node, "test", "-f", "/var/log/kubernetes/audit.log"])
        if check.returncode == 0:
            break
        import time

        time.sleep(1)
    subprocess.run(["docker", "exec", node, "chmod", "644", "/var/log/kubernetes/audit.log"], check=True)


def main() -> None:
    if CLUSTER_NAME in _existing_clusters():
        print(f"cluster {CLUSTER_NAME!r} already exists -- nothing to do")
        return
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    config_path = render_config()
    subprocess.run(["kind", "create", "cluster", "--name", CLUSTER_NAME, "--config", str(config_path)], check=True)
    fix_audit_log_permissions()
    print(f"cluster {CLUSTER_NAME!r} ready, kubectl context={KUBE_CONTEXT!r}")


if __name__ == "__main__":
    sys.exit(main())
