"""Portable tests for execution_k8s.py's naming helpers -- no cluster, no kubectl needed. The
real Pod-creating path is tested separately (test_execution_k8s_cluster.py, k8s_cluster marker)."""
from __future__ import annotations

from siphonophore_core.execution_k8s import label_value_for, pod_name_for


def test_pod_name_for_is_a_valid_rfc1123_label():
    name = pod_name_for("deleg-vertical-001")
    assert name.startswith("sipho-deleg-vertical-001-")
    assert len(name) <= 63
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert not name.startswith("-") and not name.endswith("-")


def test_pod_name_for_sanitizes_unsafe_characters():
    name = pod_name_for("Weird_ID.with/slashes!!")
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)


def test_pod_name_for_two_calls_never_collide():
    a = pod_name_for("same-execution-id")
    b = pod_name_for("same-execution-id")
    assert a != b


def test_pod_name_for_empty_slug_still_produces_a_name():
    name = pod_name_for("!!!")
    assert name.startswith("sipho-exec-")


def test_label_value_for_strips_unsafe_characters_and_length():
    value = label_value_for("weird/value with spaces" + "x" * 80)
    assert len(value) <= 63
    assert all(c.isalnum() or c in "_.-" for c in value)
