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


def test_planning_profile_prompt_discloses_search_repository_noise_exclusion():
    """Capability truth extends to what search_repository will and won't surface
    (siphonophore_core/execution_readonly.py's hardcoded-exclude + .gitignore filtering) -- the
    model should not conclude a search "failed" just because .git/.venv/node_modules content it
    can see via read_file/list_directory never turns up in search_repository matches."""
    prompt = build_system_prompt(planning_profile(root="."))
    assert ".gitignore" in prompt
    assert "never searched" in prompt


def test_planning_profile_prompt_forbids_artifact_code_entirely():
    prompt = build_system_prompt(planning_profile(root="."))
    assert "MUST NOT be present for any kind in this session" in prompt


def test_planning_profile_prompt_example_never_uses_artifact_code():
    prompt = build_system_prompt(planning_profile(root="."))
    example_line = next(line for line in prompt.splitlines() if '"operation": {"kind": "read_file"' in line)
    assert "artifact_code" not in example_line


# ---- V1.1: the model must not be told it has a small per-turn operation budget -------------------
# (the reproduced human-trial defect: the old wording ("within a bounded per-turn limit") primed
# the model to ration/apologize for a multi-step investigation instead of just completing it).

def test_prompt_does_not_claim_a_small_bounded_per_turn_operation_limit():
    for prompt in (DEFAULT_SYSTEM_PROMPT, build_system_prompt(planning_profile(root="."))):
        assert "bounded per-turn limit" not in prompt


def test_prompt_encourages_requesting_as_many_operations_as_genuinely_needed():
    for prompt in (DEFAULT_SYSTEM_PROMPT, build_system_prompt(planning_profile(root="."))):
        assert "as many operations" in prompt
        assert "safety-net" in prompt


# ---- capability-truthful "consequence" field: KindExecutionPolicy profiles must not be told a ---
# ---- caller-declared consequence tier drives execution-class routing, since it does not ---------

def test_portable_profile_prompt_states_consequence_is_load_bearing():
    """portable_profile() is ConsequencePolicy-routed: intent.consequence genuinely selects the
    execution class, so the prompt's own honest-self-assessment framing is truthful here."""
    prompt = build_system_prompt(portable_profile())
    assert "no independent check behind it" in prompt
    assert "NOT load-bearing" not in prompt


def test_planning_profile_prompt_states_consequence_is_not_load_bearing():
    """planning_profile() is KindExecutionPolicy-routed (composition.py): KindExecutionPolicy.
    evaluate() never reads intent.consequence at all (policy.py) -- the prompt must say so, not
    repeat the portable profile's "your honest assessment... this is currently taken as you
    declare it" framing, which would actively mislead the model about what its own declared field
    does in this session."""
    prompt = build_system_prompt(planning_profile(root="."))
    assert "NOT load-bearing in this session" in prompt
    assert "no independent check behind it" not in prompt


def test_planning_profile_prompt_does_not_call_the_kind_mapping_a_consequence_mapping():
    """The KindExecutionPolicy mapping's keys are Intent.kind values (e.g. "read_file"), not
    consequence tiers -- the capability-disclosure prose must not label it "consequence-to-
    execution-class", which would misrepresent a kind as if it were a declared trust tier."""
    prompt = build_system_prompt(planning_profile(root="."))
    assert "consequence-to-execution-class" not in prompt
    assert '"read_file" -> "read_file"' in prompt


def test_portable_profile_prompt_still_calls_the_mapping_a_consequence_mapping():
    """Regression guard for the branch above: portable_profile() (ConsequencePolicy) keeps its
    existing, still-accurate "consequence-to-execution-class" wording."""
    prompt = build_system_prompt(portable_profile())
    assert "consequence-to-execution-class" in prompt


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
