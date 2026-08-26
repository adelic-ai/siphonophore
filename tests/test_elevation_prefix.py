"""Tests for _elevation_prefix() -- portable, no root/Linux needed, since it's a pure decision
based on os.geteuid(). The real proof that useradd/userdel actually work through the wrapper
scripts, elevated or not, is linux_root_only (test_execution_uid_cgroup.py's existing suite
exercises the "already root, no elevation" path for real on every colima run; a genuinely
unprivileged-plus-sudo run is its own separate, explicit validation -- see HISTORY.md)."""
from __future__ import annotations

import os

from siphonophore_core.execution_uid_cgroup import _elevation_prefix


def test_no_elevation_when_already_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert _elevation_prefix() == ()


def test_sudo_dash_n_when_not_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    assert _elevation_prefix() == ("sudo", "-n")
