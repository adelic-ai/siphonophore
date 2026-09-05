"""One immutable evidence directory per attempt.

Pre-registration mapping: the evidence-directory discipline -- fresh IDs, never overwrite a failed
attempt, preserve the starting/ending authority snapshots, the normalized mediated response, every
bypass case result, the audit slice, live-object state, credential PRESENCE/FINGERPRINT metadata
only, timestamps, and the exact Siphonophore (and AgentWatch, if used) commits.

Immutability is enforced mechanically, not by convention:
  * `create()` uses `os.mkdir`, which fails if the directory exists. A collision is an error, never
    a silent reuse.
  * `write_json()` refuses to overwrite an existing file.
  * Every write passes through `redaction.assert_no_secrets` first, so a credential-shaped value
    fails the run instead of being persisted.

Nothing here writes inside the repository. The default root is outside the working tree, because a
preserved attempt is evidence, not source, and Stage 2 kept the same separation.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import redaction

DEFAULT_EVIDENCE_ROOT = "/tmp/sipho-bypass-evidence"


class AttemptCollisionError(RuntimeError):
    """An attempt directory already exists. Never overwritten -- a failed attempt is evidence."""


class ImmutableWriteError(RuntimeError):
    """Something tried to rewrite a file already recorded for this attempt."""


def new_attempt_id(prefix: str = "bypass") -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def _git_commit(repo: str | Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


@dataclass
class AttemptDirectory:
    root: str = DEFAULT_EVIDENCE_ROOT
    attempt_id: str = field(default_factory=new_attempt_id)
    _written: set[str] = field(default_factory=set, repr=False)

    @property
    def path(self) -> Path:
        return Path(self.root) / self.attempt_id

    def create(self) -> Path:
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.mkdir(mode=0o700)          # os.mkdir semantics: fails if it already exists
        except FileExistsError as exc:
            raise AttemptCollisionError(f"attempt directory already exists: {target}") from exc
        return target

    def write_json(self, name: str, obj: Any) -> Path:
        """Write one immutable, secret-scanned JSON artifact."""
        if not name.endswith(".json"):
            name = f"{name}.json"
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"artifact name must be a plain filename: {name!r}")
        target = self.path / name
        if name in self._written or target.exists():
            raise ImmutableWriteError(f"refusing to overwrite existing evidence artifact: {target}")
        text = redaction.safe_json_dumps(obj)      # raises SecretLeakError rather than redacting
        with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(text)
            handle.write("\n")
        self._written.add(name)
        return target

    def provenance(self, *, siphonophore_repo: str | Path, agentwatch_repo: str | Path | None = None,
                   extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Exactly which code produced this attempt. Recorded before anything else, so a preserved
        attempt is interpretable without the working tree it came from."""
        return {
            "attempt_id": self.attempt_id,
            "created_at_unix": time.time(),
            "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "siphonophore_repo": str(siphonophore_repo),
            "siphonophore_commit": _git_commit(siphonophore_repo),
            "agentwatch_repo": str(agentwatch_repo) if agentwatch_repo else None,
            "agentwatch_commit": _git_commit(agentwatch_repo) if agentwatch_repo else None,
            "python": os.sys.version.split()[0],
            "extra": dict(extra or {}),
        }

    def manifest(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "artifacts": sorted(self._written),
            "finalized_at_unix": time.time(),
        }


def load_attempt(path: str | Path) -> dict[str, Any]:
    """Read a preserved attempt back. Read-only; never mutates what it loads."""
    directory = Path(path)
    out: dict[str, Any] = {}
    for artifact in sorted(directory.glob("*.json")):
        try:
            out[artifact.name] = json.loads(artifact.read_text())
        except ValueError:
            out[artifact.name] = {"_unparseable": True}
    return out
