"""Experiment-local machinery for the Kubernetes mediation-bypass experiment.

STATUS: implementation only. Nothing here has been executed as a scientific experiment, and
nothing here provisions the deployment it targets (see PROVISIONING_SPEC.md).

This package lives under `experiments/`, is not imported by `siphonophore_core` or
`siphonophore_harness`, is not on `pyproject.toml`'s `testpaths`, and adds no dependency to either
project. It composes the REAL Siphonophore mediation path (`Gate`/`Executor`/`Broker`/
`K8sPodBackend`) unchanged -- it never reimplements, wraps around, or bypasses any of it.

Read README.md (the pre-registration) before reading this code. Every module here exists to serve
a criterion or falsification case named there, and the mapping is stated in each module docstring.
"""

PACKAGE_NAME = "sipho_bypass"
