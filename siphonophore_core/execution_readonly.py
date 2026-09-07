"""Read-only, root-confined observational ExecutionBackends (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md):
capabilities a typed cognitive/planning agent needs to look at a filesystem without ever being able
to write to it, run arbitrary code, or escape a configured root directory.

These are ordinary `ExecutionBackend` implementations -- the same extension point
`SameProcessBackend`/`SeparateProcessBackend`/`UidCgroupBackend` already use (`execution.py`,
DESIGN.md section 6) -- registered under execution classes that match their own `Intent.kind`
one-to-one via `policy.KindExecutionPolicy`, not derived from a declared `consequence` tier the way
`ConsequencePolicy` derives `same_process`/`separate_process`/`uid_cgroup`. Nothing about that
routing choice is specific to any one harness; it lives in core because `Policy`/`ExecutionBackend`
are both already core, pluggable extension points, not because these three operations are
Siphonophore's "identity" -- a caller free to ignore all three and use `ConsequencePolicy` with
`SameProcessBackend`/`SeparateProcessBackend` instead, exactly as before this module existed.

Every backend here is confined to a `root` directory supplied at construction time (never derived
from `intent.payload`, so a completion cannot widen its own confinement) and bounded (byte/entry/
match counts, truncated with an explicit marker, never silently). No backend in this module
executes code of any kind, spawns a process, or opens anything for writing.
"""
from __future__ import annotations

import re
from pathlib import Path

from .execution import ExecutionBackend, ExecutionError
from .intent import Effect, Intent
from .policy import Decision

_MAX_READ_BYTES = 200_000
_MAX_LISTING_ENTRIES = 2_000
_MAX_SEARCH_MATCHES = 500
_MAX_MATCH_LINE_CHARS = 500
_MAX_FILES_WALKED = 5_000


class PathEscapesRootError(ExecutionError):
    """A requested path resolves outside this backend's configured root directory -- refused
    before any filesystem access is attempted. Distinct from an ordinary not-found error: this is
    a confinement violation, not a missing-file condition."""


def _confine(root: Path, relative_path: str) -> Path:
    """Resolves `relative_path` against `root` and confirms the result is still inside `root`.
    Real confinement, not a naming convention: `Path.resolve()` follows symlinks, so a symlink
    inside `root` that points outside it is caught here by the post-resolve containment check, not
    merely by a textual "does the string contain .." scan. Rejects absolute paths outright -- a
    caller-declared absolute path has no business inside a root-confined operation, regardless of
    where it would resolve to."""
    if Path(relative_path).is_absolute():
        raise PathEscapesRootError(
            f"path must be relative to the configured root, got an absolute path: {relative_path!r}"
        )
    resolved_root = root.resolve()
    candidate = (resolved_root / relative_path).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise PathEscapesRootError(f"path {relative_path!r} resolves outside the configured root")
    return candidate


class ReadFileBackend(ExecutionBackend):
    """Reads one text file, confined to `root`, bounded at `max_bytes`. Implements only `run()`,
    which never opens a file for anything but reading -- there is no write path in this class at
    all, not merely an unused one.

    Guarantees actually made: path confinement (`_confine`, real and symlink-aware); a bounded
    read, truncated with an explicit marker, never silent. Guarantees NOT made: no judgment about
    whether `root` itself was configured to include files the caller shouldn't have exposed (a
    composition-time choice, not this backend's concern); no protection against binary content
    (decoded as UTF-8 with `errors="replace"`, not rejected -- deciding what counts as "text" is
    left to the caller reading the result, not silently guessed here); no root/euid refusal the way
    `SameProcessBackend`/`SeparateProcessBackend` have -- those checks exist because those backends
    run ARBITRARY caller-authored code with full process privilege, whereas this backend's own
    behavior is fixed and narrow (read one confined file); running the broker as root would let
    this backend read root-owned files within `root`, which is a property of how `root` was
    configured, not something an euid check on this class would change."""

    def __init__(self, root: str | Path, max_bytes: int = _MAX_READ_BYTES) -> None:
        self._root = Path(root)
        self._max_bytes = max_bytes

    def run(self, decision: Decision, intent: Intent) -> Effect:
        path = intent.payload.get("path")
        if not path:
            raise ExecutionError("read_file requires a non-empty 'path' in payload")
        confined = _confine(self._root, path)
        if not confined.is_file():
            raise ExecutionError(f"no such file: {path!r}")
        raw = confined.read_bytes()
        truncated = len(raw) > self._max_bytes
        text = raw[: self._max_bytes].decode("utf-8", errors="replace")
        if truncated:
            text += f"\n...[truncated, file exceeds {self._max_bytes} bytes]"
        return Effect(
            intent_id=intent.intent_id, execution_class="read_file",
            detail={"path": path, "content": text, "truncated": truncated},
        )


class ListDirectoryBackend(ExecutionBackend):
    """Lists one directory's immediate entries (non-recursive), confined to `root`, bounded at
    `max_entries`. Never recurses -- a caller wanting a deeper listing issues more than one
    operation, each independently mediated, matching this architecture's "every real effect
    independently mediated" invariant rather than one call silently walking an entire tree."""

    def __init__(self, root: str | Path, max_entries: int = _MAX_LISTING_ENTRIES) -> None:
        self._root = Path(root)
        self._max_entries = max_entries

    def run(self, decision: Decision, intent: Intent) -> Effect:
        path = intent.payload.get("path") or "."
        confined = _confine(self._root, path)
        if not confined.is_dir():
            raise ExecutionError(f"no such directory: {path!r}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in confined.iterdir())
        truncated = len(entries) > self._max_entries
        return Effect(
            intent_id=intent.intent_id, execution_class="list_directory",
            detail={"path": path, "entries": entries[: self._max_entries], "truncated": truncated},
        )


class SearchRepositoryBackend(ExecutionBackend):
    """Searches for a regular-expression `pattern` (Python `re`, matched per line) under `root`,
    optionally narrowed to a `path` subdirectory -- confined the same way `ReadFileBackend`/
    `ListDirectoryBackend` are. Bounded at `max_matches` and per-line at `_MAX_MATCH_LINE_CHARS`;
    also bounded on how many files it will walk (`_MAX_FILES_WALKED`) so a huge tree cannot make a
    single search run unboundedly long even before the match bound is reached.

    No shell, no subprocess, no external `grep`/`ripgrep` dependency -- a plain Python walk plus
    `re.search` per line, so this backend needs no separate sandboxing story beyond the path
    confinement and bounds already stated. A file that cannot be read as text (a genuine OS error,
    e.g. a broken symlink) is silently skipped, not treated as a match failure."""

    def __init__(self, root: str | Path, max_matches: int = _MAX_SEARCH_MATCHES) -> None:
        self._root = Path(root)
        self._max_matches = max_matches

    def run(self, decision: Decision, intent: Intent) -> Effect:
        pattern = intent.payload.get("pattern")
        if not pattern:
            raise ExecutionError("search_repository requires a non-empty 'pattern' in payload")
        subdir = intent.payload.get("path") or "."
        confined = _confine(self._root, subdir)
        if not confined.is_dir():
            raise ExecutionError(f"no such directory to search: {subdir!r}")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ExecutionError(f"invalid search pattern: {exc}") from exc

        resolved_root = self._root.resolve()
        matches: list[dict] = []
        files_walked = 0
        truncated_matches = False
        truncated_files = False
        for file_path in sorted(confined.rglob("*")):
            if not file_path.is_file():
                continue
            files_walked += 1
            if files_walked > _MAX_FILES_WALKED:
                truncated_files = True
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    rel = file_path.resolve().relative_to(resolved_root)
                    matches.append({"path": str(rel), "line": line_no, "text": line[:_MAX_MATCH_LINE_CHARS]})
                    if len(matches) >= self._max_matches:
                        truncated_matches = True
                        break
            if truncated_matches:
                break
        return Effect(
            intent_id=intent.intent_id, execution_class="search_repository",
            detail={
                "pattern": pattern, "matches": matches,
                "truncated": truncated_matches or truncated_files,
            },
        )
