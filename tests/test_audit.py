"""Tests for the audit module's reconciliation logic. Entirely portable -- reconcile(),
reconcile_path(), and collect_ground_truth() are plain Python with no Linux or root dependency.
A genuine root-required scenario (a real uid_cgroup sub-agent lying about its own effects) is
exercised in test_audit_linux.py, matching lab/007's real-delegation predicate."""
from __future__ import annotations

from pathlib import Path

from siphonophore_core.audit import BelnapValue, Claim, SelfReport, collect_ground_truth, reconcile, reconcile_path


def test_reconcile_truth_table():
    assert reconcile(True, True) == BelnapValue.CORROBORATED
    assert reconcile(True, False) == BelnapValue.CONTRADICTION
    assert reconcile(False, True) == BelnapValue.UNREPORTED_ACTIVITY
    assert reconcile(False, False) == BelnapValue.NO_EVIDENCE


def test_self_report_from_dict():
    report = SelfReport.from_dict({
        "principal_id": "alice",
        "claims": [{"path": "a.txt", "content": "hello"}, {"path": "b.txt", "content": "world"}],
    })
    assert report.principal_id == "alice"
    assert report.claims == (Claim(path="a.txt", content="hello"), Claim(path="b.txt", content="world"))


def test_self_report_from_dict_defaults():
    report = SelfReport.from_dict({})
    assert report.principal_id == "unknown"
    assert report.claims == ()


def test_reconcile_path_corroborated():
    report = SelfReport(principal_id="alice", claims=(Claim(path="a.txt", content="X"),))
    result = reconcile_path(report, "a.txt", "X", ground_truth={"a.txt": "X"})
    assert result["value"] == "corroborated"
    assert result["claimed"] is True
    assert result["observed"] is True


def test_reconcile_path_contradiction_same_path_different_content():
    """The claim and the ground truth both mention the same path, but the actual content differs
    from what was claimed -- a real, active lie, not merely 'path exists or not'."""
    report = SelfReport(principal_id="alice", claims=(Claim(path="a.txt", content="what alice claims"),))
    result = reconcile_path(report, "a.txt", "what alice claims", ground_truth={"a.txt": "what actually happened"})
    assert result["value"] == "contradiction"
    assert result["claimed"] is True
    assert result["observed"] is False
    assert result["self_reported_content"] == "what alice claims"
    assert result["ground_truth_content"] == "what actually happened"


def test_reconcile_path_unreported_activity():
    report = SelfReport(principal_id="alice", claims=())
    result = reconcile_path(report, "extra.txt", None, ground_truth={"extra.txt": "surprise"})
    assert result["value"] == "unreported_activity"
    assert result["claimed"] is False
    assert result["observed"] is True


def test_reconcile_path_no_evidence():
    report = SelfReport(principal_id="alice", claims=())
    result = reconcile_path(report, "never.txt", None, ground_truth={})
    assert result["value"] == "no_evidence"
    assert result["claimed"] is False
    assert result["observed"] is False


def test_collect_ground_truth_reads_files_only_not_subdirs(tmp_path: Path):
    (tmp_path / "a.txt").write_text("A")
    (tmp_path / "b.txt").write_text("B")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "c.txt").write_text("C")  # not collected -- top-level only

    ground_truth = collect_ground_truth(tmp_path)
    assert ground_truth == {"a.txt": "A", "b.txt": "B"}


def test_collect_ground_truth_empty_dir(tmp_path: Path):
    assert collect_ground_truth(tmp_path) == {}
