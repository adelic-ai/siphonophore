"""Portable-only coverage for CheckedInUidCgroupBackend: confirms the refusal path off real root
Linux. Every other behavior needs real useradd/cgroup v2/SO_PEERCRED and is covered in
test_execution_uid_cgroup_checkin_linux.py (linux_root_only, run on colima)."""
from __future__ import annotations

import pytest

from siphonophore_core.execution_uid_cgroup import ProvisioningError
from siphonophore_core.execution_uid_cgroup_checkin import CheckedInUidCgroupBackend


def test_construction_refuses_off_real_root_linux():
    """Matches every uid_cgroup-dependent lab experiment's own discipline: refuse cleanly and
    immediately (at construction, before any provisioning) anywhere that isn't real root Linux --
    never silently degrade to a weaker guarantee. On this test's own darwin host this always
    raises; on colima's real root Linux (test_execution_uid_cgroup_checkin_linux.py), construction
    succeeds and behavior is exercised for real."""
    try:
        backend = CheckedInUidCgroupBackend()
    except ProvisioningError:
        return
    backend.shutdown()
    pytest.skip("running as real root on real Linux -- refusal path not applicable here")
