"""Explicit, separate cleanup for the experiment's kind cluster -- never invoked automatically by
setup_cluster.py or by the experiment tests, matching this repo's own established convention
(execution_k8s.py's Pods aren't auto-deleted either; cleanup is always a deliberate, separate step)."""
from __future__ import annotations

import subprocess
import sys

from setup_cluster import CLUSTER_NAME


def main() -> None:
    subprocess.run(["kind", "delete", "cluster", "--name", CLUSTER_NAME], check=True)


if __name__ == "__main__":
    sys.exit(main())
