"""Installed-code integrity and R's authority snapshot -- all with temporary fixtures.

Pre-registration mapping: the CRITICAL TRUST RULE (the mediator must not import R-writable code),
criteria 1/7/8, and falsification cases F-01, F-02, F-03, F-09, F-11.
"""
from __future__ import annotations

import os
import stat

import pytest

from sipho_bypass import integrity
from sipho_bypass.evidence import Verdict
from sipho_bypass.requester import authority_snapshot as snap_mod

R_UID, R_GIDS = 1001, [1001]
M_UID, M_GID = 1002, 1002


# --- the DAC predicate ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,st_uid,st_gid,expected", [
    (0o644, M_UID, M_GID, False),                    # M-owned, R has no write bit
    (0o646, M_UID, M_GID, True),                     # world-writable
    (0o664, M_UID, R_GIDS[0], True),                 # R's group can write
    (0o644, R_UID, M_GID, True),                     # R owns it (u+w)
    (0o444, R_UID, M_GID, False),                    # R owns it but read-only
    (0o600, M_UID, M_GID, False),                    # the intended kubeconfig shape
    (0o777, M_UID, M_GID, True),
])
def test_writability_predicate(mode, st_uid, st_gid, expected):
    assert integrity.mode_is_writable_by(mode=mode, st_uid=st_uid, st_gid=st_gid,
                                         uid=R_UID, gids=R_GIDS) is expected


def test_a_writable_parent_directory_defeats_a_readonly_file(tmp_path):
    """The classic failure: a root-owned 0644 file inside a directory R can write is replaceable
    regardless of its own mode. An ancestor walk is the only way to see it."""
    parent = tmp_path / "writable"
    parent.mkdir(mode=0o777)
    target = parent / "mediator.py"
    target.write_text("print('x')")
    target.chmod(0o444)
    report = integrity.verify_not_writable_by(target, uid=os.getuid(), gids=os.getgroups())
    assert report.writable_by_subject is False
    assert str(parent) in report.writable_ancestors
    assert report.safe_for_privileged_import is False


def test_a_locked_down_path_is_safe_for_privileged_import(tmp_path):
    parent = tmp_path / "pinned"
    parent.mkdir(mode=0o755)
    target = parent / "mediator.py"
    target.write_text("print('x')")
    target.chmod(0o644)
    # Checked from the perspective of a DIFFERENT uid that owns nothing here.
    report = integrity.verify_not_writable_by(target, uid=999999, gids=[999999])
    assert report.writable_by_subject is False
    assert report.writable_ancestors == () or all(
        not p.startswith(str(tmp_path)) for p in report.writable_ancestors)


def test_missing_path_reports_absence_not_safety(tmp_path):
    report = integrity.verify_not_writable_by(tmp_path / "nope", uid=R_UID, gids=R_GIDS)
    assert report.exists is False
    assert report.safe_for_privileged_import is False


def test_report_declares_its_own_limitations():
    report = integrity.PathIntegrityReport(path="/x", exists=False)
    joined = " ".join(report.limitations)
    assert "ACL" in joined and "root" in joined


# --- manifests -------------------------------------------------------------------------------------

def test_manifest_detects_modification_addition_and_removal(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("original\n")
    (tmp_path / "pkg" / "b.py").write_text("b\n")
    baseline = integrity.build_manifest(tmp_path)
    assert integrity.verify_manifest(tmp_path, baseline).ok

    (tmp_path / "pkg" / "a.py").write_text("TAMPERED\n")
    (tmp_path / "pkg" / "c.py").write_text("c\n")
    (tmp_path / "pkg" / "b.py").unlink()
    diff = integrity.verify_manifest(tmp_path, baseline)
    assert diff.modified == ("pkg/a.py",)
    assert diff.unexpected == ("pkg/c.py",)
    assert diff.missing == ("pkg/b.py",)
    assert not diff.ok


def test_manifest_excludes_pycache_so_it_is_deterministic(tmp_path):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.py").write_text("derived\n")
    (tmp_path / "real.py").write_text("real\n")
    assert list(integrity.build_manifest(tmp_path)) == ["real.py"]


def test_manifest_of_the_real_experiment_package_is_reproducible():
    import pathlib
    root = pathlib.Path(integrity.__file__).resolve().parent
    assert integrity.build_manifest(root) == integrity.build_manifest(root)


# --- import path ------------------------------------------------------------------------------------

def test_empty_sys_path_entry_is_flagged():
    """`''` means cwd, which under `sudo -u M` is R's own directory -- a direct R-controlled import
    source. `-I` removes it, and this is how that is verified rather than assumed."""
    report = integrity.import_path_report([""], uid=R_UID, gids=R_GIDS, environ={})
    assert report.has_empty_entry is True and report.clean is False


def test_pythonpath_is_flagged():
    report = integrity.import_path_report([], uid=R_UID, gids=R_GIDS, environ={"PYTHONPATH": "/tmp"})
    assert report.pythonpath_set is True and report.clean is False


def test_r_writable_sys_path_entry_is_flagged(tmp_path):
    d = tmp_path / "rwx"
    d.mkdir(mode=0o777)
    report = integrity.import_path_report([str(d)], uid=os.getuid(), gids=os.getgroups(), environ={})
    assert str(d) in report.writable_entries and report.clean is False


def test_clean_import_path_reports_clean(tmp_path):
    d = tmp_path / "ro"
    d.mkdir(mode=0o555)
    report = integrity.import_path_report([str(d)], uid=999999, gids=[999999], environ={})
    assert report.clean is True


# --- authority snapshot -------------------------------------------------------------------------------

def test_snapshot_records_measurable_identity_facts():
    snap = snap_mod.take_snapshot()
    assert snap.uid == os.getuid() and snap.euid == os.geteuid()
    assert snap.username is not None
    assert snap.group_ids == tuple(sorted(os.getgroups()))


def test_snapshot_records_what_it_did_and_did_not_search():
    snap = snap_mod.take_snapshot()
    assert snap.credential_paths_searched
    assert any("not a host scanner" in s for s in snap.credential_paths_not_searched)


def test_unreadable_file_records_a_failing_read_not_just_a_mode(tmp_path):
    """Criterion 1 wants a positive measurement. `os.access` alone answers a question about bits."""
    secret = tmp_path / "m.kubeconfig"
    secret.write_text("apiVersion: v1\n")
    secret.chmod(0o000)
    fact = snap_mod.inspect_path(str(secret))
    assert fact.exists is True
    if os.getuid() != 0:                     # root can read anything; the VM user is not root
        assert fact.readable is False
        assert fact.read_errno is not None


def test_readable_file_is_reported_readable_without_retaining_content(tmp_path):
    p = tmp_path / "k"
    p.write_text("SENSITIVE-CONTENT-SHOULD-NOT-APPEAR")
    fact = snap_mod.inspect_path(str(p))
    assert fact.readable is True
    assert "SENSITIVE" not in str(fact.__dict__)


def test_credential_case_fails_when_the_mediator_kubeconfig_is_readable(tmp_path):
    readable = tmp_path / "m.kubeconfig"
    readable.write_text("apiVersion: v1\n")
    snap = snap_mod.take_snapshot(mediator_kubeconfig=str(readable))
    case = snap_mod.credential_custody_case(snap, mediator_kubeconfig=str(readable))
    assert case.verdict is Verdict.FAIL
    assert str(readable) in case.observations["readable_credential_files"]


def test_credential_case_passes_when_nothing_enumerated_is_readable(tmp_path):
    snap = snap_mod.take_snapshot(mediator_kubeconfig=str(tmp_path / "absent.kubeconfig"))
    case = snap_mod.credential_custody_case(snap, mediator_kubeconfig=str(tmp_path / "absent.kubeconfig"))
    assert case.verdict is Verdict.PASS
    assert case.observations["readable_credential_files"] == []
    assert "never 'no credential exists anywhere'" in case.notes


def test_runtime_authority_case_tolerates_absent_runtime_tooling():
    """No Docker or containerd is installed in the development VM; detection must still work."""
    case = snap_mod.runtime_authority_case(snap_mod.take_snapshot())
    assert case.verdict is Verdict.PASS
    assert case.observations["readable_runtime_sockets"] == []
    assert case.observations["sockets_examined"]


def test_runtime_authority_case_fails_on_group_membership():
    snap = snap_mod.take_snapshot()
    snap.runtime_group_membership = ("docker",)
    case = snap_mod.runtime_authority_case(snap)
    assert case.verdict is Verdict.FAIL


def test_snapshot_comparison_detects_privilege_expansion():
    """Criterion 8."""
    before = snap_mod.take_snapshot()
    after = snap_mod.take_snapshot()
    assert snap_mod.compare_snapshots(before, after)["identical"] is True

    after.group_names = tuple(sorted(set(after.group_names) | {"docker"}))
    after.runtime_group_membership = ("docker",)
    diff = snap_mod.compare_snapshots(before, after)
    assert diff["identical"] is False
    assert "runtime_group_membership" in diff["differences"]


def test_comparison_ignores_time_but_not_authority():
    import time
    before = snap_mod.take_snapshot()
    time.sleep(0.01)
    after = snap_mod.take_snapshot()
    assert before.taken_at != after.taken_at
    assert snap_mod.compare_snapshots(before, after)["identical"] is True


def test_snapshot_is_json_serializable_and_secret_free():
    from sipho_bypass import redaction
    redaction.safe_json_dumps(snap_mod.take_snapshot().to_dict())
