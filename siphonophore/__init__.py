"""siphonophore -- real per-node OS identity for Strands multi-agent orchestrators.

See DESIGN.md at the repo root for the full design reasoning. This package is built bottom-up,
primitives first: identity provisioning (identity.py, checkin.py) before the orchestrator
(orchestrator.py) that wires them into per-node dispatch.
"""
from __future__ import annotations

from .orchestrator import (
    Colony,
    CheckinTimeoutError,
    DuplicateNodeError,
    OrchestratorError,
    RecipeError,
    SeveredRecipe,
    UnknownNodeError,
)

__all__ = [
    "Colony",
    "SeveredRecipe",
    "OrchestratorError",
    "DuplicateNodeError",
    "UnknownNodeError",
    "RecipeError",
    "CheckinTimeoutError",
]
