"""Put this experiment's package on sys.path for its own cluster-free tests.

Follows the same shape Stage 1/Stage 2 used: these tests are deliberately OUTSIDE
`pyproject.toml`'s `testpaths` (which is `tests/` only), so a bare `pytest` from the repository
root does not collect them. Run them explicitly:

    .venv/bin/python -m pytest experiments/k8s_mediation_bypass/ -v

Nothing here requires a cluster, a credential, Docker, kind, kubectl, bpftrace, AgentWatch, or any
privilege.
"""
from __future__ import annotations

import sys
from pathlib import Path

EXPERIMENT_ROOT = Path(__file__).resolve().parent
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))
