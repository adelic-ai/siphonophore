"""Tests for default_child_env() -- portable, no root/Linux needed, since it's pure dict
filtering. The real proof that this actually closes the environment-inheritance gap (a spawned
child no longer seeing the broker's secrets) is linux_root_only, in
test_execution_uid_cgroup_linux_env.py, since it needs a real subprocess under a real dropped-
privilege uid to mean anything."""
from __future__ import annotations

from siphonophore_core.execution_uid_cgroup import DEFAULT_CHILD_ENV_KEYS, default_child_env


def test_only_allowlisted_keys_pass_through():
    source = {"PATH": "/usr/bin", "LANG": "en_US.UTF-8", "ANTHROPIC_API_KEY": "sk-should-not-leak"}
    result = default_child_env(source)
    assert result == {"PATH": "/usr/bin", "LANG": "en_US.UTF-8"}
    assert "ANTHROPIC_API_KEY" not in result


def test_missing_allowlisted_keys_are_simply_absent_not_errors():
    result = default_child_env({"PATH": "/usr/bin"})
    assert result == {"PATH": "/usr/bin"}


def test_empty_source_yields_empty_env():
    assert default_child_env({}) == {}


def test_default_source_is_the_real_process_environment(monkeypatch):
    monkeypatch.setenv("PATH", "/real/path")
    monkeypatch.setenv("SOME_SECRET", "should-not-appear")
    result = default_child_env()  # no source given -- reads real os.environ
    assert result.get("PATH") == "/real/path"
    assert "SOME_SECRET" not in result


def test_allowlist_is_exactly_the_documented_keys():
    """Pin the allowlist itself -- silently growing it later (e.g. someone adding a convenience
    key) should be a deliberate, reviewed change, not a side effect of an unrelated edit."""
    assert set(DEFAULT_CHILD_ENV_KEYS) == {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
