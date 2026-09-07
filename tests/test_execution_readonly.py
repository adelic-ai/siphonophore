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
    assert effect.detail["truncated"] is True


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


def test_search_repository_bounded_matches(sandbox):
    lots_dir = sandbox / "lots"
    lots_dir.mkdir()
    (lots_dir / "lots.txt").write_text("\n".join(f"MARKER_TOKEN line {i}" for i in range(50)))
    backend = SearchRepositoryBackend(root=sandbox, max_matches=3)
    effect = backend.run(None, _intent("search_repository", {"pattern": "MARKER_TOKEN", "path": "lots"}))
    assert len(effect.detail["matches"]) == 3
    assert effect.detail["truncated"] is True


def test_search_repository_path_must_be_a_directory(sandbox):
    backend = SearchRepositoryBackend(root=sandbox)
    with pytest.raises(ExecutionError, match="no such directory to search"):
        backend.run(None, _intent("search_repository", {"pattern": "x", "path": "README.md"}))


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
