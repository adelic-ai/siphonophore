"""DEFAULT_SYSTEM_PROMPT / build_system_prompt() -- teach a real model the turn-envelope protocol
intent_parsing.py's parse_turn()/parse_intent()/parse_work_order() expect. Necessary, not
decorative: without it, a real model given a raw user message has no reason to respond with the
expected JSON envelope instead of ordinary conversational text, and the very first real turn fails
on IntentParseError before anything interesting happens. This is the harness's half of the
contract -- parse_turn() enforces the schema; this prompt is what gives a real model a reason to
satisfy it.

`build_system_prompt(profile)` exists because the schema alone is not the whole truth a model
needs -- capability truth (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): a model must be told what
the ACTIVE session can really do, derived from `profile.allowed_kinds`/`execution_classes`/
`policy_mapping`, never a fixed, hardcoded vocabulary. `portable_profile()` (run_artifact/
write_file, code-bearing) and `planning_profile()` (read_file/list_directory/search_repository,
typed, no code at all) describe completely different operation vocabularies, and this module must
never claim a kind is available, or that a kind needs/forbids artifact_code, other than what the
given profile actually allows.
"""
from __future__ import annotations

from .composition import ExecutionProfile, portable_profile
from .intent_parsing import CODE_BEARING_KINDS

_ENVELOPE_SCHEMA_PROMPT = """You are an agent with no direct ability to take any action. You cannot \
write files, run code, fetch external content, or affect anything in the world by yourself. Your \
only capability is to optionally describe ONE requested operation per response, which a separate \
authorization system evaluates and, if permitted, carries out on your behalf. You will be told \
what actually happened afterward, and may then answer the user's original question or request \
another operation -- request as many operations, in sequence, as you genuinely need to answer the \
user's actual question well; there is no small budget to ration against, and a real, multi-step \
investigation completing in one exchange is normal, not something to apologize for or cut short. \
A very large safety-net ceiling exists purely as a backstop against a genuine runaway (requesting \
far more operations than any ordinary question could need) -- it is not a per-question budget, and \
an ordinary question will never come close to it. Stop requesting operations and answer as soon as \
you actually have enough to answer -- not because of any operation count.

Respond with a single JSON object and nothing else -- no markdown fence, nothing before or after \
it. The object has these top-level fields, all independently optional, EXCEPT that "operation" and \
"work_order" may never both be present in the same response:

  "message": plain text for the human -- an explanation, a conversational reply, whatever you'd \
normally say. Never executed, never evaluated, purely for the human to read. Omit it if you have \
nothing to say.
  "operation": an object describing the one effect you want to happen this turn. Omit "operation" \
entirely (do not send null or an empty object) if you have nothing you want to do this turn -- \
ordinary conversation needs no operation at all, and there is no need to invent one just to have \
something to send.
  "work_order": present only once you and the user have converged on a fully-specified piece of \
work meant for someone else to carry out later -- see WORK ORDERS below. Never present alongside \
"operation" in the same response.

When "operation" is present, it must be an object with these fields:

  "kind": one of {kind_list}
  "payload": an object of parameters relevant to what you're doing (may be empty: {{}})
  "consequence": one of "low", "high", "privileged" -- {consequence_field_rule}
  "artifact_code": {artifact_code_rule}

{kind_descriptions}
Example -- message only, no operation requested:
{{"message": "The capital of Japan is Tokyo."}}

{operation_example}

WORK ORDERS: once a conversation converges on a fully-specified piece of work, you may compile a \
"work_order" object instead of an "operation" (a "message" may still accompany it). Required \
fields: "status" ("draft" while still being refined and open to revision, "final" only once the \
user has explicitly confirmed convergence -- do not send "final" merely because a lot of \
conversation happened), "objective" (one sentence), "prompt" (the complete, self-contained \
instructions a future implementer would need, written so it stands alone WITHOUT access to this \
conversation). Optional fields, each a list of short strings unless noted: "requirements", \
"constraints", "acceptance_criteria", "context", "prohibited_scope", "expected_artifacts", \
"capability_requirements", "data_requirements", "network_requirements", "credential_requirements", \
"isolation_requirements", "resource_expectations" (a short string, not a list), \
"decomposition_suggestions". Compiling a work_order NEVER itself performs any work, grants any \
authority, or launches anything -- it is a structured proposal; what happens to it is entirely up \
to the human and whatever system reads it afterward.

Do not invent fields outside this schema (in particular, never include an "intent_id", "token", \
"decision", or "work_order_id" field anywhere -- those are assigned by the authorization system, \
not by you, and including them will cause your response to be rejected outright).

{capability_prose}"""

# Per-kind blurbs shown only for kinds the active profile actually allows (capability truth). An
# allowed kind absent from this dict still gets a generic, honest fallback line -- never silently
# omitted, never fabricated detail this module doesn't actually know.
_KIND_DESCRIPTIONS = {
    "run_artifact": (
        '"run_artifact" runs the Python source given as "artifact_code" -- "kind" itself is just '
        "a label used for policy, not a code selector."
    ),
    "write_file": (
        '"write_file" also runs "artifact_code" (there is no separate file-writing primitive) -- '
        "artifact_code must itself contain the Python that opens and writes the file, e.g. "
        "\"with open('/path', 'w') as f:\\n    f.write('content')\"."
    ),
    "read_file": (
        '"read_file" reads one text file. payload MUST include "path" (relative to this session\'s '
        'configured root -- never an absolute path, never one that escapes the root via "..").'
    ),
    "list_directory": (
        '"list_directory" lists one directory\'s immediate entries (never recursive). payload MAY '
        'include "path" (relative to the configured root; omit for the root itself).'
    ),
    "search_repository": (
        '"search_repository" searches for a regular-expression "pattern" (payload, REQUIRED) '
        'across text files under the configured root, optionally narrowed with payload "path". '
        "Version-control/dependency/cache directories (.git, .venv, __pycache__, node_modules, "
        "and similar) and anything the root's own .gitignore excludes are never searched -- a "
        "missing match there means it was intentionally skipped as noise, not that the search "
        "failed."
    ),
}
_UNKNOWN_KIND_DESCRIPTION = (
    '"{kind}" is available in this session, but this prompt has no further description of its '
    "payload shape -- consult whatever documentation this session's operator has provided."
)

_EXAMPLE_ARTIFACT_CODE = "with open('/tmp/example.txt', 'w') as f:\\n    f.write('hello')"

_CONSEQUENCE_FIELD_RULE_LOAD_BEARING = (
    "your honest assessment of how much authority/risk this specific action requires. Be honest "
    "here: this is currently taken as you declare it, with no independent check behind it -- "
    "there is no verification catching an under-declared consequence, so getting this right is "
    "entirely on you assessing your own action truthfully, not a safety net you can rely on."
)
_CONSEQUENCE_FIELD_RULE_INERT = (
    "accepted for schema compatibility, but NOT load-bearing in this session: every kind you can "
    "request here has a fixed, single execution path regardless of what you put here (see the "
    "capability description below for the actual kind -> execution-class mapping this session "
    "uses). Declare your honest assessment anyway if you have one, but do not expect it to change "
    "what happens -- it does not."
)


def _consequence_field_rule(consequence_is_load_bearing: bool) -> str:
    return _CONSEQUENCE_FIELD_RULE_LOAD_BEARING if consequence_is_load_bearing else _CONSEQUENCE_FIELD_RULE_INERT


def _artifact_code_rule(allowed_kinds: tuple[str, ...]) -> str:
    code_bearing = sorted(k for k in allowed_kinds if k in CODE_BEARING_KINDS)
    typed = sorted(k for k in allowed_kinds if k not in CODE_BEARING_KINDS)
    if code_bearing and not typed:
        return "REQUIRED whenever \"operation\" is present -- Python source code to execute."
    if code_bearing and typed:
        return (
            f"REQUIRED for {code_bearing} (Python source code to execute) -- MUST NOT be present "
            f"for any other kind in this session ({typed}); each of those has fixed behavior "
            "described below, never controlled by code you supply."
        )
    return (
        "MUST NOT be present for any kind in this session -- none of them execute arbitrary code; "
        "each kind's behavior is fixed (see the description of each kind below)."
    )


def _kind_descriptions_block(allowed_kinds: tuple[str, ...]) -> str:
    lines = [
        _KIND_DESCRIPTIONS.get(kind, _UNKNOWN_KIND_DESCRIPTION.format(kind=kind))
        for kind in sorted(allowed_kinds)
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _operation_example(allowed_kinds: tuple[str, ...]) -> str:
    """One concrete, schema-valid example using a kind this profile actually allows -- never a
    kind that would be rejected, so the example itself is never misleading."""
    if "run_artifact" in allowed_kinds:
        return (
            'Example -- an operation, with a message alongside it:\n'
            '{"message": "Sure, doing that now.", "operation": {"kind": "run_artifact", '
            '"payload": {}, "consequence": "low", "artifact_code": "' + _EXAMPLE_ARTIFACT_CODE + '"}}'
        )
    if "read_file" in allowed_kinds:
        return (
            'Example -- an operation, with a message alongside it:\n'
            '{"message": "Let me check that file.", "operation": {"kind": "read_file", '
            '"payload": {"path": "README.md"}, "consequence": "low"}}'
        )
    if allowed_kinds:
        example_kind = sorted(allowed_kinds)[0]
        return (
            f'Example -- an operation, with a message alongside it:\n'
            f'{{"message": "Doing that now.", "operation": {{"kind": {example_kind!r}, '
            f'"payload": {{}}, "consequence": "low"}}}}'
        )
    return "(This session has no operation kinds available at all -- every turn is conversation-only.)"


def _capability_prose(profile: ExecutionProfile) -> str:
    """Truthful, profile-derived capability disclosure -- distinguishes "this is policy-mapped to
    that execution class" (`profile.policy_mapping`, always true, substrate-independent) from
    "that execution class is actually registered and can run something right now"
    (`profile.execution_classes`, true only for backends this specific profile wired up). Never
    claims a mapped-but-unregistered execution class (e.g. "uid_cgroup" under the portable
    profile) is deliverable.

    `profile.policy_mapping` has the identical `{str: str}` shape regardless of which `Policy`
    backs the profile, but what the KEYS actually ARE differs: a declared consequence tier
    (`"low"`/`"high"`/`"privileged"`) for a `ConsequencePolicy`-based profile, versus an
    `Intent.kind` for a `KindExecutionPolicy`-based one (composition.py's own
    `consequence_is_load_bearing` states which). Describing the latter as a "consequence-to-
    execution-class" mapping would tell the model its own declared `consequence` field is what
    selects the execution class here, which is false -- `KindExecutionPolicy.evaluate()` never
    reads `intent.consequence` at all (policy.py). This branches on that same flag so the
    vocabulary in the prompt matches which policy is actually active."""
    mapping_prose = ", ".join(
        f'"{key}" -> "{execution_class}"' for key, execution_class in sorted(profile.policy_mapping.items())
    )
    registered = ", ".join(sorted(profile.execution_classes))
    unavailable = sorted(set(profile.policy_mapping.values()) - set(profile.execution_classes))

    if profile.consequence_is_load_bearing:
        mapping_description = (
            f"This session's active profile is {profile.name!r}. Its consequence-to-execution-class "
            f"policy mapping is: {mapping_prose}."
        )
    else:
        mapping_description = (
            f"This session's active profile is {profile.name!r}. Each operation kind you can request "
            f"has a fixed execution class, independent of the \"consequence\" field you declare "
            f"(see that field's own description above): {mapping_prose}."
        )

    lines = [
        mapping_description,
        f"Only these execution classes are actually registered and available in this session "
        f"right now: {registered}.",
    ]
    if unavailable:
        lines.append(
            "The policy mapping above names an execution class with NO backend registered in "
            "this session (" + ", ".join(unavailable) + "). A consequence tier that maps to it is "
            "recognized by policy but not deliverable here -- honestly declaring that consequence "
            "will be authorized and then rejected as backend-unavailable, not silently run under a "
            "different, less-privileged execution class."
        )
    return "\n".join(lines)


def build_system_prompt(profile: ExecutionProfile) -> str:
    """The model-facing system prompt for a session running under `profile`: the envelope schema,
    the WorkOrder mechanism, and a truthful account of that profile's own allowed operation kinds,
    registered execution classes, and policy mapping. Parsing itself (intent_parsing.py) takes no
    profile argument and is unaffected by this -- only prompt text generation is profile-derived."""
    allowed_kinds = tuple(profile.allowed_kinds)
    kind_list = ", ".join(f'"{k}"' for k in sorted(allowed_kinds)) if allowed_kinds else "(none available in this session)"
    return _ENVELOPE_SCHEMA_PROMPT.format(
        kind_list=kind_list,
        consequence_field_rule=_consequence_field_rule(profile.consequence_is_load_bearing),
        artifact_code_rule=_artifact_code_rule(allowed_kinds),
        kind_descriptions=_kind_descriptions_block(allowed_kinds),
        operation_example=_operation_example(allowed_kinds),
        capability_prose=_capability_prose(profile),
    )


DEFAULT_SYSTEM_PROMPT = build_system_prompt(portable_profile())
