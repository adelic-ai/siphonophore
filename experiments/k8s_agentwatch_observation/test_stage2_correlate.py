"""Ordinary, portable, canned-evidence unit tests for stage2_correlate.py -- no cluster, no
privilege, no network. Run as part of the normal pre-experiment testing order, before touching
the real fixture."""
from __future__ import annotations

from stage2_correlate import container_id_from_cgroup_path, normalize_container_id

REAL_PATH_SHAPE = (
    "/system.slice/docker-3350ade4226f0adcd9e4e9545d7c2a16a5aa362d12315f2a88e2024cea1246cf.scope/"
    "kubelet.slice/kubelet-kubepods.slice/kubelet-kubepods-besteffort.slice/"
    "kubelet-kubepods-besteffort-podb6a16766_05ee_446a_8ee5_ff96a817f47c.slice/"
    "cri-containerd-679f3691f7f1b20198548be43b86f265f9fff9be0ac4c973638f5ee89decbca9.scope"
)


def test_container_id_extracted_from_real_shaped_path():
    assert container_id_from_cgroup_path(REAL_PATH_SHAPE) == (
        "679f3691f7f1b20198548be43b86f265f9fff9be0ac4c973638f5ee89decbca9"
    )


def test_container_id_none_for_higher_level_slice():
    higher = REAL_PATH_SHAPE.rsplit("/", 1)[0]  # the pod-level slice, no cri-containerd leaf
    assert container_id_from_cgroup_path(higher) is None


def test_normalize_strips_containerd_prefix():
    raw = "containerd://679f3691f7f1b20198548be43b86f265f9fff9be0ac4c973638f5ee89decbca9"
    assert normalize_container_id(raw) == "679f3691f7f1b20198548be43b86f265f9fff9be0ac4c973638f5ee89decbca9"


def test_normalize_passthrough_when_no_prefix():
    bare = "679f3691f7f1b20198548be43b86f265f9fff9be0ac4c973638f5ee89decbca9"
    assert normalize_container_id(bare) == bare


def test_exact_match_required_not_fuzzy():
    """Falsification rule: a near-miss (one changed hex digit) must NOT be treated as equal --
    this test just documents/enforces that stage2_correlate never does substring/prefix matching
    for the final comparison, only exact string equality (asserted by the caller, not here)."""
    a = container_id_from_cgroup_path(REAL_PATH_SHAPE)
    b = a[:-1] + ("0" if a[-1] != "0" else "1")
    assert a != b
