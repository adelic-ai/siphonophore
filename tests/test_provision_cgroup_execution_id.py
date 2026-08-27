"""Portable test for provision_cgroup()'s execution_id path-safety check -- no root/Linux needed,
since this is pure validation logic that runs before any real filesystem call. Mirrors the
identical check spawn_helper/siphonophore-spawn.c applies to the same construction (SH-17);
see that file's own valid_execution_id() and DESIGN.md/HISTORY.md for why both sides need to
agree."""
from __future__ import annotations

from pathlib import Path

import pytest

from siphonophore_core.execution_uid_cgroup import ProvisioningError, provision_cgroup


@pytest.mark.parametrize("bad_id", ["../../etc", "a/b", "", "x" * 64, "with space", "trailing/"])
def test_rejects_unsafe_execution_ids(tmp_path: Path, bad_id: str):
    with pytest.raises(ProvisioningError, match="path-safety"):
        provision_cgroup(tmp_path, bad_id)


@pytest.mark.parametrize("good_id", ["a", "exec-001", "A1_b-2", "x" * 63])
def test_accepts_safe_execution_ids(tmp_path: Path, good_id: str):
    cg = provision_cgroup(tmp_path, good_id)
    assert cg == tmp_path / f"exec-{good_id}"
    assert cg.is_dir()
