from __future__ import annotations

from siphonophore_core.execution import ExecutionBackend
from siphonophore_core.intent import Effect
from siphonophore_harness.composition import compose_profile, planning_profile, portable_profile
from siphonophore_harness.intent_parsing import OPERATION_ALLOWED_FIELDS
from siphonophore_harness.prompts import DEFAULT_SYSTEM_PROMPT, build_system_prompt


def test_default_system_prompt_names_every_top_level_field():
    for field in ("message", "operation"):
        assert f'"{field}"' in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_names_exactly_the_operation_fields_parse_intent_allows():
    """Catches drift in either direction: a field parse_intent accepts inside "operation" but the
    prompt never mentions (the model has no reason to use it), or a field the prompt implies
    exists but parse_intent would reject as unknown."""
    for field in OPERATION_ALLOWED_FIELDS:
        assert f'"{field}"' in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_names_the_real_default_kinds_and_consequences():
    """Must match ConsequencePolicy's actual default vocabulary (policy.py) -- an invented
    vocabulary here would just teach a real model to describe operations the default policy
    denies."""
    for kind in ("write_file", "run_artifact"):
        assert kind in DEFAULT_SYSTEM_PROMPT
    for consequence in ("low", "high", "privileged"):
        assert consequence in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_warns_against_the_fields_parse_intent_rejects():
    assert "intent_id" in DEFAULT_SYSTEM_PROMPT
    assert "token" in DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_requires_artifact_code_whenever_operation_is_present():
    assert "REQUIRED whenever" in DEFAULT_SYSTEM_PROMPT or "required" in DEFAULT_SYSTEM_PROMPT.lower()


# ---- build_system_prompt(): profile-derived capability disclosure -------------------------------

def test_default_system_prompt_is_built_from_the_portable_profile():
    assert DEFAULT_SYSTEM_PROMPT == build_system_prompt(portable_profile())


def test_build_system_prompt_names_portable_profiles_registered_execution_classes():
    prompt = build_system_prompt(portable_profile())
    assert "same_process" in prompt
    assert "separate_process" in prompt


def test_build_system_prompt_does_not_claim_uid_cgroup_is_available_under_portable():
    """ConsequencePolicy.DEFAULT_MAPPING maps "privileged" to "uid_cgroup" regardless of profile,
    but portable_profile() registers no such backend (composition.py). The prompt may still
    mention uid_cgroup (it's a real policy-mapping target), but only as explicitly unavailable --
    never implying it is a deliverable, registered execution class."""
    prompt = build_system_prompt(portable_profile())
    assert "uid_cgroup" in prompt  # named as part of the honest policy-mapping disclosure
    assert "not deliverable" in prompt or "NO backend registered" in prompt
    registered_line = next(line for line in prompt.splitlines() if "actually registered and available" in line)
    assert "uid_cgroup" not in registered_line


class _FakeBackend(ExecutionBackend):
    def run(self, decision, intent) -> Effect:
        return Effect(intent_id=intent.intent_id, execution_class="widget_class", detail={})


def test_build_system_prompt_is_derived_from_the_given_profile_not_hardcoded_to_portable():
    """Falsifies the "combine" design decision if build_system_prompt()'s output were identical
    regardless of which ExecutionProfile is passed to it."""
    custom_profile = compose_profile(
        "widget-profile",
        backends={"widget_class": _FakeBackend()},
        policy_mapping={"low": "widget_class"},
    )
    prompt = build_system_prompt(custom_profile)
    assert "widget_class" in prompt
    assert "widget-profile" in prompt
    assert "same_process" not in prompt
    assert "separate_process" not in prompt


def test_build_system_prompt_for_a_profile_with_no_unavailable_mapping_omits_the_warning():
    custom_profile = compose_profile(
        "fully-available-profile",
        backends={"widget_class": _FakeBackend()},
        policy_mapping={"low": "widget_class"},
    )
    prompt = build_system_prompt(custom_profile)
    assert "not deliverable" not in prompt
    assert "NO backend registered" not in prompt


# ---- planning profile: typed-operation, no-code capability truth ---------------------------------

def test_planning_profile_prompt_names_only_its_own_typed_kinds():
    prompt = build_system_prompt(planning_profile(root="."))
    for kind in ("read_file", "list_directory", "search_repository"):
        assert kind in prompt
    assert "run_artifact" not in prompt
    assert "write_file" not in prompt


def test_planning_profile_prompt_forbids_artifact_code_entirely():
    prompt = build_system_prompt(planning_profile(root="."))
    assert "MUST NOT be present for any kind in this session" in prompt


def test_planning_profile_prompt_example_never_uses_artifact_code():
    prompt = build_system_prompt(planning_profile(root="."))
    example_line = next(line for line in prompt.splitlines() if '"operation": {"kind": "read_file"' in line)
    assert "artifact_code" not in example_line


# ---- work_order mechanism is always described, regardless of profile -----------------------------

def test_prompt_describes_the_work_order_mechanism():
    for prompt in (DEFAULT_SYSTEM_PROMPT, build_system_prompt(planning_profile(root="."))):
        assert '"work_order"' in prompt
        assert '"status"' in prompt
        assert '"objective"' in prompt
        assert '"prompt"' in prompt
        assert "grants any authority" in prompt or "NEVER itself performs any work" in prompt


def test_prompt_warns_operation_and_work_order_are_mutually_exclusive():
    assert "never both be present" in DEFAULT_SYSTEM_PROMPT.lower() or "Never present alongside" in DEFAULT_SYSTEM_PROMPT
