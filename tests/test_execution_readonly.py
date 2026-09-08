"""Tests for the read-only, root-confined observational ExecutionBackends
(siphonophore_core/execution_readonly.py): ReadFileBackend, ListDirectoryBackend,
SearchRepositoryBackend. Portable -- no root, no network, no subprocess."""
from __future__ import annotations

import pytest

from siphonophore_core.execution_readonly import (
    ListDirectoryBackend,
    PathEscapesRootError,
    ReadFileBackend,
    SearchRepositoryBackend,
)
from siphonophore_core.execution import ExecutionError
from siphonophore_core.intent import Intent
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import KindExecutionPolicy


def _intent(kind: str, payload: dict) -> Intent:
    return Intent(kind=kind, principal_id="alice", intent_id="i-1", consequence="low", payload=payload)


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def hello():\n    print('hi')\n")
    (tmp_path / "src" / "nested").mkdir()
    (tmp_path / "src" / "nested" / "deep.py").write_text("MARKER_TOKEN = 1\n")
    (tmp_path / "README.md").write_text("# Project\nMARKER_TOKEN appears here too.\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("should never be reachable")
    yield tmp_path
    outside.unlink(missing_ok=True)


# ---- ReadFileBackend ------------------------------------------------------------------------

def test_read_file_returns_content(sandbox):
    backend = ReadFileBackend(root=sandbox)
    decision = None
    intent = _intent("read_file", {"path": "src/main.py"})
    effect = backend.run(decision, intent)
    assert effect.execution_class == "read_file"
    assert "def hello" in effect.detail["content"]
    assert effect.detail["truncated"] is False


def test_read_file_truncates_large_files(sandbox):
    big = sandbox / "big.txt"
    big.write_text("A" * 1000)
    backend = ReadFileBackend(root=sandbox, max_bytes=100)
    effect = backend.run(None, _intent("read_file", {"path": "big.txt"}))
    assert effect.detail["truncated"] is True
    assert "truncated" in effect.detail["content"]
    assert len(effect.detail["content"]) < 1000


def test_read_file_rejects_absolute_path(sandbox):
    backend = ReadFileBackend(root=sandbox)
    with pytest.raises(PathEscapesRootError):
        backend.run(None, _intent("read_file", {"path": "/etc/passwd"}))


def test_read_file_rejects_dotdot_escape(sandbox):
    backend = ReadFileBackend(root=sandbox)
    with pytest.raises(PathEscapesRootError):
        backend.run(None, _intent("read_file", {"path": f"../{sandbox.name}-outside-secret.txt"}))


def test_read_file_rejects_symlink_escape(sandbox):
    link = sandbox / "escape_link"
    link.symlink_to(sandbox.parent / f"{sandbox.name}-outside-secret.txt")
    backend = ReadFileBackend(root=sandbox)
    with pytest.raises(PathEscapesRootError):
        backend.run(None, _intent("read_file", {"path": "escape_link"}))


def test_read_file_missing_path_field_raises():
    backend = ReadFileBackend(root=".")
    with pytest.raises(ExecutionError, match="requires a non-empty 'path'"):
        backend.run(None, _intent("read_file", {}))


def test_read_file_nonexistent_file_raises(sandbox):
    backend = ReadFileBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="no such file"):
        backend.run(None, _intent("read_file", {"path": "does_not_exist.txt"}))


def test_read_file_non_string_path_fails_closed_not_a_raw_type_error(sandbox):
    """Reproduced defect: a non-string, truthy `path` (e.g. a JSON array) skipped the
    `if not path` empty-check and reached `Path(relative_path)` inside `_confine()`, raising a raw
    `TypeError` that CognitiveLoop.step()'s `except (GateViolation, ExecutionError, IdentityError)`
    does not catch -- escaping the dispatch handling boundary entirely instead of failing closed."""
    backend = ReadFileBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="path must be a string"):
        backend.run(None, _intent("read_file", {"path": ["src", "main.py"]}))


# ---- ListDirectoryBackend ---------------------------------------------------------------------

def test_list_directory_returns_entries(sandbox):
    backend = ListDirectoryBackend(root=sandbox)
    effect = backend.run(None, _intent("list_directory", {"path": "src"}))
    assert effect.execution_class == "list_directory"
    assert "main.py" in effect.detail["entries"]
    assert "nested/" in effect.detail["entries"]


def test_list_directory_defaults_to_root(sandbox):
    backend = ListDirectoryBackend(root=sandbox)
    effect = backend.run(None, _intent("list_directory", {}))
    assert "src/" in effect.detail["entries"]
    assert "README.md" in effect.detail["entries"]


def test_list_directory_does_not_recurse(sandbox):
    backend = ListDirectoryBackend(root=sandbox)
    effect = backend.run(None, _intent("list_directory", {"path": "src"}))
    assert "deep.py" not in effect.detail["entries"]  # nested/deep.py, not surfaced at this level


def test_list_directory_rejects_escape(sandbox):
    backend = ListDirectoryBackend(root=sandbox)
    with pytest.raises(PathEscapesRootError):
        backend.run(None, _intent("list_directory", {"path": ".."}))


def test_list_directory_bounded(sandbox):
    many = sandbox / "many"
    many.mkdir()
    for i in range(20):
        (many / f"f{i}.txt").write_text("x")
    backend = ListDirectoryBackend(root=sandbox, max_entries=5)
    effect = backend.run(None, _intent("list_directory", {"path": "many"}))
    assert len(effect.detail["entries"]) == 5


def test_list_directory_non_string_path_fails_closed_not_a_raw_type_error(sandbox):
    backend = ListDirectoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="path must be a string"):
        backend.run(None, _intent("list_directory", {"path": {"nope": True}}))


# ---- SearchRepositoryBackend --------------------------------------------------------------------

def test_search_repository_finds_matches(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN"}))
    assert effect.execution_class == "search_repository"
    paths = {m["path"] for m in effect.detail["matches"]}
    assert "src/nested/deep.py" in paths
    assert "README.md" in paths


def test_search_repository_scoped_to_subdirectory(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN", "path": "src"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"src/nested/deep.py"}


def test_search_repository_rejects_escape(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(PathEscapesRootError):
        backend.run(None, _intent("search_repository", {"pattern": "x", "path": "/etc"}))


def test_search_repository_invalid_regex_raises_execution_error(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="invalid search pattern"):
        backend.run(None, _intent("search_repository", {"pattern": "("}))


def test_search_repository_missing_pattern_raises(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="requires a non-empty 'pattern'"):
        backend.run(None, _intent("search_repository", {}))


def test_search_repository_non_string_pattern_fails_closed_not_a_raw_type_error(sandbox):
    """Reproduced defect: a non-string, truthy `pattern` (e.g. a JSON array) skipped the
    `if not pattern` check and reached `re.compile(pattern)`/dict-keying internals, raising a raw
    `TypeError` rather than failing closed with `ExecutionError`."""
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="requires a non-empty 'pattern'"):
        backend.run(None, _intent("search_repository", {"pattern": ["MARKER_TOKEN"]}))


def test_search_repository_non_string_path_fails_closed_not_a_raw_type_error(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="path must be a string"):
        backend.run(None, _intent("search_repository", {"pattern": "x", "path": 123}))


def test_search_repository_bounded_matches(sandbox):
    lots_dir = sandbox / "lots"
    lots_dir.mkdir()
    (lots_dir / "lots.txt").write_text("\n".join(f"MARKER_TOKEN line {i}" for i in range(50)))
    backend = SearchRepositoryBackend(root=sandbox, max_matches=3)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN", "path": "lots"}))
    assert len(effect.detail["matches"]) == 3
    assert effect.detail["truncated"] is True


def test_search_repository_path_may_name_a_single_file(sandbox):
    """A `path` naming one existing file searches just that file, not the whole tree -- a genuine
    ergonomics widening, not a confinement/bound relaxation (V1.1: reproduced a real trial where
    the model tried this, got the old ExecutionError, and had to recover by falling back to a
    directory search)."""
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN", "path": "README.md"}))
    assert [m["path"] for m in effect.detail["matches"]] == ["README.md"]
    assert effect.detail["truncated"] is False


def test_search_repository_single_file_path_never_reads_other_files(sandbox):
    """Confirms the single-file mode is genuinely scoped to that one file -- src/nested/deep.py
    also contains MARKER_TOKEN but must never appear in a search explicitly scoped to README.md."""
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN", "path": "README.md"}))
    assert all(m["path"] == "README.md" for m in effect.detail["matches"])


def test_search_repository_path_must_exist(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="no such file or directory to search"):
        backend.run(None, _intent("search_repository", {"pattern": "x", "path": "does-not-exist"}))


# ---- SearchRepositoryBackend: confinement re-checked on every recursively-discovered entry -----
# (a symlink placed INSIDE root that resolves outside it is not confined merely because the
# top-level search path was; a bare `rglob` walk read such a file's content before ever checking
# this, crashing with an unhandled ValueError after the read had already happened -- reproduced and
# fixed, see execution_readonly.py's `_iter_confined_files`).

def test_search_repository_does_not_read_through_a_symlinked_file_that_escapes_root(sandbox):
    outside = sandbox.parent / f"{sandbox.name}-outside-dir"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("SECRET_MARKER should never be reachable\n")
    (sandbox / "escape_file_link.txt").symlink_to(secret)
    try:
        backend = SearchRepositoryBackend(root=sandbox)
        effect = backend.run(None, _intent("search_repository", {"pattern": "SECRET_MARKER"}))
        assert effect.detail["matches"] == []  # never read, let alone returned
    finally:
        secret.unlink()
        outside.rmdir()


def test_search_repository_does_not_descend_through_a_symlinked_directory_that_escapes_root(sandbox):
    outside = sandbox.parent / f"{sandbox.name}-outside-dir2"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET_MARKER should never be reachable\n")
    (sandbox / "escape_dir_link").symlink_to(outside)
    try:
        backend = SearchRepositoryBackend(root=sandbox)
        effect = backend.run(None, _intent("search_repository", {"pattern": "SECRET_MARKER"}))
        assert effect.detail["matches"] == []
    finally:
        (outside / "secret.txt").unlink()
        outside.rmdir()


def test_search_repository_symlink_escape_does_not_consume_files_walked_or_match_budget(sandbox):
    """A skipped escaping entry must not count against _MAX_FILES_WALKED/max_matches -- it was
    never genuinely searched, so it must not crowd out real content the way the reproduced defect
    (matches from noise directories consuming the truncation budget) did."""
    outside = sandbox.parent / f"{sandbox.name}-outside-dir3"
    outside.mkdir()
    (outside / "secret.txt").write_text("MARKER_TOKEN\n")
    (sandbox / "escape_link").symlink_to(outside)
    try:
        backend = SearchRepositoryBackend(root=sandbox)
        effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN"}))
        paths = {m["path"] for m in effect.detail["matches"]}
        assert "src/nested/deep.py" in paths
        assert "README.md" in paths
        assert not any("escape_link" in p for p in paths)
    finally:
        (outside / "secret.txt").unlink()
        outside.rmdir()


# ---- SearchRepositoryBackend: adversarial symlink topology (cycles) and the file-count bound ----
# Reproduced/characterized (post-V1.1 hardening review): the walk previously fully materialized
# and sorted every confined, non-excluded entry BEFORE applying `_MAX_FILES_WALKED` -- an
# adversarially large (but acyclic) tree paid the full walk/sort cost regardless of the bound
# (measured: ~1.9s / 60,000 files vs ~0.3s after the fix, and the cost is proportional to total
# tree size, not the bound, so this scales unboundedly with an attacker-controlled tree). A
# self-referential or two-node cyclic directory symlink topology happened NOT to hang in practice
# only because the OS's own per-lookup symlink-resolution limit (ELOOP) incidentally terminated
# the ever-lengthening resolved path after a few dozen iterations -- not something this module's
# own correctness should depend on. Both are fixed together: `_iter_confined_files` now tracks
# visited real directory identities (deterministic cycle termination, independent of any OS
# limit), and `SearchRepositoryBackend.run()` bounds how many entries it ever pulls from the
# (lazy) walk, rather than materializing it fully before truncating.

def test_search_repository_self_referential_directory_symlink_terminates(sandbox):
    """A directory containing a symlink to itself: is_dir() is followed for a directory symlink,
    so without cycle protection this can re-descend into itself forever."""
    (sandbox / "a").mkdir()
    (sandbox / "a" / "loop").symlink_to(sandbox / "a", target_is_directory=True)
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "hello", "path": "a"}))
    assert effect.detail["truncated"] is False
    assert effect.detail["matches"] == []


def test_search_repository_two_node_cyclic_directory_symlinks_terminates(sandbox):
    """A <-> B via two directory symlinks, neither pointing directly at itself -- an indirect
    cycle self-referential-only cycle protection could miss."""
    (sandbox / "cyc_a").mkdir()
    (sandbox / "cyc_b").mkdir()
    (sandbox / "cyc_b" / "marker.txt").write_text("MARKER_TOKEN\n")
    (sandbox / "cyc_a" / "to_b").symlink_to(sandbox / "cyc_b", target_is_directory=True)
    (sandbox / "cyc_b" / "to_a").symlink_to(sandbox / "cyc_a", target_is_directory=True)
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN", "path": "cyc_a"}))
    assert effect.detail["truncated"] is False
    # cyc_b (and its real marker.txt) is reachable via cyc_a/to_b -- found once, not looped over
    paths = [m["path"] for m in effect.detail["matches"]]
    assert paths.count("cyc_b/marker.txt") == 1


def test_search_repository_symlinked_directory_resolving_inside_root_is_searched_once(sandbox):
    """A symlink pointing at another location already inside root is real, legitimate confinement
    (not an escape) -- it is followed, but visited-tracking means its target's contents are
    reported once, not duplicated via both the direct path and the alias."""
    (sandbox / "alias_to_src").symlink_to(sandbox / "src", target_is_directory=True)
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN"}))
    paths = [m["path"] for m in effect.detail["matches"]]
    assert paths.count("src/nested/deep.py") == 1


def test_search_repository_large_tree_beyond_file_count_bound_is_truncated_without_full_materialization(sandbox):
    from siphonophore_core.execution_readonly import _MAX_FILES_WALKED

    many = sandbox / "many"
    many.mkdir()
    for i in range(_MAX_FILES_WALKED + 10):
        (many / f"f{i}.txt").write_text("nomatch\n")
    backend = SearchRepositoryBackend(root=sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "NEVERMATCH", "path": "many"}))
    assert effect.detail["truncated"] is True
    assert effect.detail["matches"] == []


# ---- SearchRepositoryBackend: noise reduction (hardcoded excludes + .gitignore) -----------------

@pytest.fixture
def noisy_sandbox(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("NOISE_MARKER real source\n")
    for noisy_dir in (".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "mypkg.egg-info"):
        d = tmp_path / noisy_dir
        d.mkdir()
        (d / "somefile").write_text("NOISE_MARKER should not surface\n")
    return tmp_path


def test_search_repository_excludes_hardcoded_noise_dirs_even_without_gitignore(noisy_sandbox):
    backend = SearchRepositoryBackend(root=noisy_sandbox)
    effect = backend.run(None, _intent("search_repository", {"pattern": "NOISE_MARKER"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"src/main.py"}


def test_search_repository_excludes_hardcoded_noise_dirs_consuming_match_budget(noisy_sandbox):
    """The exact shape of the reproduced human-trial defect: a small max_matches bound must not be
    consumed by alphabetically-early noise directories (e.g. ".git" sorts before "src")."""
    backend = SearchRepositoryBackend(root=noisy_sandbox, max_matches=1)
    effect = backend.run(None, _intent("search_repository", {"pattern": "NOISE_MARKER"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"src/main.py"}


def test_search_repository_excludes_caller_supplied_extra_dir_names(tmp_path):
    """V1.1: a real trial's search_repository fanned into siphonophore-sessions/*.jsonl (a prior
    session's own generated transcript) and surfaced it as if it were ordinary repository
    evidence. `extra_excluded_dir_names` is the generic, caller-configurable mechanism a harness
    (composition.py's planning_profile()) uses to exclude its own generated-artifact directories
    without hardcoding a harness-specific name into this core, harness-neutral module."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("SESSION_MARKER real source\n")
    (tmp_path / "siphonophore-sessions").mkdir()
    (tmp_path / "siphonophore-sessions" / "s1.jsonl").write_text('{"SESSION_MARKER": "stale transcript"}\n')

    backend = SearchRepositoryBackend(root=tmp_path, extra_excluded_dir_names=("siphonophore-sessions",))
    effect = backend.run(None, _intent("search_repository", {"pattern": "SESSION_MARKER"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"src/main.py"}


def test_search_repository_respects_project_gitignore(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("GI_MARKER kept\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.txt").write_text("GI_MARKER excluded by gitignore\n")
    (tmp_path / "notes.local.md").write_text("GI_MARKER excluded by gitignore too\n")
    (tmp_path / ".gitignore").write_text("build/\n*.local.md\n")

    backend = SearchRepositoryBackend(root=tmp_path)
    effect = backend.run(None, _intent("search_repository", {"pattern": "GI_MARKER"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"src/main.py"}


def test_search_repository_gitignore_negation_reincludes(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.log").write_text("LOG_MARKER excluded\n")
    (tmp_path / "logs" / "keep.log").write_text("LOG_MARKER kept via negation\n")
    (tmp_path / ".gitignore").write_text("*.log\n!logs/keep.log\n")

    backend = SearchRepositoryBackend(root=tmp_path)
    effect = backend.run(None, _intent("search_repository", {"pattern": "LOG_MARKER"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"logs/keep.log"}


def test_search_repository_hardcoded_excludes_apply_even_when_gitignore_omits_them(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("OMIT_MARKER kept\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("OMIT_MARKER excluded regardless\n")
    # a real .gitignore that says nothing at all about node_modules
    (tmp_path / ".gitignore").write_text("*.tmp\n")

    backend = SearchRepositoryBackend(root=tmp_path)
    effect = backend.run(None, _intent("search_repository", {"pattern": "OMIT_MARKER"}))
    paths = {m["path"] for m in effect.detail["matches"]}
    assert paths == {"src/main.py"}


# ---- KindExecutionPolicy + real Gate integration (no Executor needed for this check) -----------

def test_kind_execution_policy_composes_with_gate_for_read_file():
    gate = Gate(KindExecutionPolicy({"read_file": "read_file"}))
    intent = _intent("read_file", {"path": "README.md"})
    decision = gate.submit(intent)
    assert decision.permitted is True
    assert decision.execution_class == "read_file"


def test_kind_execution_policy_denies_unregistered_kind_via_gate():
    gate = Gate(KindExecutionPolicy({"read_file": "read_file"}))
    intent = _intent("run_artifact", {})
    decision = gate.submit(intent)
    assert decision.permitted is False
