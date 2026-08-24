"""siphonophore -- real per-node OS identity for Strands multi-agent orchestrators.

See DESIGN.md at the repo root for the full design reasoning. This package is built bottom-up,
primitives first: identity provisioning (this module's siblings) before anything wires them into
an actual orchestrator.
"""
from __future__ import annotations
