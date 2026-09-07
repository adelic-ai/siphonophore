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

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from .execution import ExecutionBackend, ExecutionError
from .intent import Effect, Intent
from .policy import Decision

_MAX_READ_BYTES = 200_000
_MAX_LISTING_ENTRIES = 2_000
_MAX_SEARCH_MATCHES = 500
_MAX_MATCH_LINE_CHARS = 500
_MAX_FILES_WALKED = 5_000

# Applied unconditionally, regardless of any project .gitignore's own content -- the highest-cost,
# most-universal noise sources a recursive repository walk hits (version-control internals,
# language-specific dependency/build/cache directories). Defense in depth, not a substitute for
# .gitignore parsing below: plenty of real repositories never bother listing these in their own
# .gitignore "because it's obviously excluded by convention" (the exact reasoning that motivated
# this list, not a guess). A directory whose name is in this set, or that ends with ".egg-info",
# is never descended into -- its contents are not walked, not read, not counted toward
# _MAX_FILES_WALKED, regardless of what any .gitignore says.
_HARDCODED_EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "node_modules", "dist", "build",
})


def _is_hardcoded_excluded_dir(name: str) -> bool:
    return name in _HARDCODED_EXCLUDED_DIR_NAMES or name.endswith(".egg-info")


@dataclass(frozen=True)
class _IgnoreRule:
    """One parsed line of a `.gitignore` file. Deliberately a small, honestly-scoped subset of
    real gitignore semantics -- see `_GitignoreRules`'s own docstring for exactly what is and is
    not supported."""

    pattern: str
    negate: bool
    dir_only: bool
    anchored: bool


def _parse_gitignore_lines(text: str) -> tuple[_IgnoreRule, ...]:
    rules: list[_IgnoreRule] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        if not line:
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        anchored = line.startswith("/") or "/" in line
        if line.startswith("/"):
            line = line[1:]
        if not line:
            continue
        rules.append(_IgnoreRule(pattern=line, negate=negate, dir_only=dir_only, anchored=anchored))
    return tuple(rules)


class _GitignoreRules:
    """A small, dependency-free, honestly-scoped subset of `.gitignore` matching -- ONE file, read
    from `root` itself, never a nested per-directory `.gitignore` (unlike real git, which consults
    every directory's own file on the way down). Supports: blank lines and `#` comments (ignored),
    `!` negation (a later matching rule re-includes a path an earlier rule excluded), a trailing
    `/` meaning "directories only", and a pattern containing `/` (leading or embedded) being
    anchored to `root` and matched against the full repo-relative path rather than just the
    basename. Pattern matching itself is Python's `fnmatch` (shell-style glob), not git's own
    matcher -- `fnmatch`'s `*` matches across path separators, which happens to make many common
    anchored patterns (e.g. `build/*.log`) behave correctly, but this is NOT a claim of exact git
    `**`-vs-`*` semantics, and negation cannot re-include a path whose ancestor directory this walk
    already declined to descend into (an excluded directory's contents are never even examined --
    matching how a real search tool asked to skip an excluded tree would behave anyway). Adequate
    for the common, highest-value patterns (`__pycache__/`, `*.pyc`, `.env`, `build/`,
    `/generated.py`); not a substitute for `git check-ignore` for anyone needing exact semantics.
    """

    def __init__(self, rules: tuple[_IgnoreRule, ...]) -> None:
        self._rules = rules

    @classmethod
    def load(cls, root: Path) -> "_GitignoreRules":
        try:
            text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return cls(())
        return cls(_parse_gitignore_lines(text))

    def is_ignored(self, rel_posix: str, name: str, *, is_dir: bool) -> bool:
        ignored = False
        for rule in self._rules:
            if rule.dir_only and not is_dir:
                continue
            target = rel_posix if rule.anchored else name
            if fnmatch.fnmatch(target, rule.pattern):
                ignored = not rule.negate
        return ignored


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


def _iter_confined_files(root: Path, start: Path, ignore_rules: _GitignoreRules):
    """Recursively yields `(entry, rel_posix)` for every regular file under `start`, confined to
    `root` -- CONFINEMENT IS CHECKED ON EVERY DISCOVERED ENTRY, BEFORE it is
    ever descended into or read, not merely when a later match happens to be formatted for output.
    This is the fix for the confinement gap a bare `start.rglob("*")` walk had: nothing here trusts
    that a path discovered by recursion is still under `root` merely because `start` itself was
    confined -- a symlink (to a file or a directory) anywhere under `start` can point outside
    `root`, and each one is independently re-resolved and re-checked, exactly like `_confine()`
    already does for a caller-supplied top-level path. An entry that resolves outside `root` is
    skipped entirely -- neither descended into (if a directory) nor read (if a file) -- silently,
    the same "don't treat an edge case as an attack requiring propagation" precedent this module
    already applies to an unreadable file's OSError. Also skips any directory excluded by
    `_is_hardcoded_excluded_dir()` or `ignore_rules` -- applied once, at the point of deciding
    whether to descend, so an excluded directory's contents are never even examined, let alone
    counted toward `_MAX_FILES_WALKED`."""
    resolved_root = root.resolve()
    stack = [start]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            try:
                resolved_entry = entry.resolve()
            except OSError:
                continue
            if resolved_entry != resolved_root and resolved_root not in resolved_entry.parents:
                continue  # escapes root (e.g. a symlink pointing outside it) -- never read, never descended into
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            rel_posix = resolved_entry.relative_to(resolved_root).as_posix()
            if is_dir:
                if _is_hardcoded_excluded_dir(entry.name) or ignore_rules.is_ignored(rel_posix, entry.name, is_dir=True):
                    continue
                stack.append(entry)
            else:
                if ignore_rules.is_ignored(rel_posix, entry.name, is_dir=False):
                    continue
                yield entry, rel_posix


class SearchRepositoryBackend(ExecutionBackend):
    """Searches for a regular-expression `pattern` (Python `re`, matched per line) under `root`,
    optionally narrowed to a `path` subdirectory -- confined the same way `ReadFileBackend`/
    `ListDirectoryBackend` are, and RE-confined on every entry the recursive walk itself discovers
    (`_iter_confined_files`), not merely on the caller-supplied top-level `path`. Bounded at
    `max_matches` and per-line at `_MAX_MATCH_LINE_CHARS`; also bounded on how many (non-excluded,
    confined) files it will walk (`_MAX_FILES_WALKED`) so a huge tree cannot make a single search
    run unboundedly long even before the match bound is reached.

    Excludes a small, hardcoded set of universal noise directories unconditionally
    (`_HARDCODED_EXCLUDED_DIR_NAMES` -- `.git`, `.venv`/`venv`, `__pycache__`, `.pytest_cache`,
    `node_modules`, `*.egg-info`, `dist`, `build`, ...), regardless of whether the searched
    repository's own `.gitignore` happens to list them, plus whatever a root-level `.gitignore`
    itself excludes (`_GitignoreRules` -- a small, honestly-scoped subset, not full git semantics;
    see its own docstring for exactly what is and isn't supported). Both exclusions apply BEFORE
    truncation, so the `max_matches`/`_MAX_FILES_WALKED` bounds are only ever consumed by genuinely
    relevant content, not alphabetically-early noise.

    No shell, no subprocess, no external `grep`/`ripgrep`/gitignore-parsing dependency -- a plain
    Python walk plus `re.search` per line, so this backend needs no separate sandboxing story
    beyond the path confinement and bounds already stated. A file that cannot be read as text (a
    genuine OS error, e.g. a broken symlink) is silently skipped, not treated as a match failure."""

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

        ignore_rules = _GitignoreRules.load(self._root)
        matches: list[dict] = []
        files_walked = 0
        truncated_matches = False
        truncated_files = False
        # Sorted by repo-relative path, matching the lexical order this backend has always
        # returned -- now over a clean (confined, excluded, ignore-filtered) file list rather than
        # a raw recursive walk, so truncation below only ever discards genuinely-searched content,
        # never noise that happened to sort first.
        walked = sorted(_iter_confined_files(self._root, confined, ignore_rules), key=lambda pair: pair[1])
        for file_path, rel_posix in walked:
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
                    matches.append({"path": rel_posix, "line": line_no, "text": line[:_MAX_MATCH_LINE_CHARS]})
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
