"""DEFAULT_SYSTEM_PROMPT / build_system_prompt() -- teach a real model the turn-envelope protocol
intent_parsing.py's parse_turn()/parse_intent() expect. Necessary, not decorative: without it, a
real model given a raw user message has no reason to respond with the expected JSON envelope
instead of ordinary conversational text, and the very first real turn fails on IntentParseError
before anything interesting happens. This is the harness's half of the contract -- parse_turn()
enforces the schema; this prompt is what gives a real model a reason to satisfy it.

`build_system_prompt(profile)` exists because the schema alone is not the whole truth a model
needs: `ConsequencePolicy.DEFAULT_MAPPING` (policy.py) maps `"privileged"` to `"uid_cgroup"`
regardless of which profile is active, but `portable_profile()` (composition.py) registers no such
backend. A model told only the static schema, with no profile-derived capability disclosure, could
honestly follow the schema, declare `"consequence": "privileged"`, and still hit
`NoBackendRegisteredError` -- an authorized-but-unexecutable turn. `build_system_prompt()` renders
the schema alongside a truthful, profile-derived account of which execution classes are actually
registered, mirroring `examples/repl.py`'s existing `render_startup_banner()` pattern rather than
inventing a second disclosure mechanism. `DEFAULT_SYSTEM_PROMPT` remains a plain module-level
constant -- `build_system_prompt(portable_profile())` -- for any caller that just wants the
ordinary reference-harness prompt without constructing a profile itself.
"""
from __future__ import annotations

from .composition import ExecutionProfile, portable_profile

_ENVELOPE_SCHEMA_PROMPT = """You are an agent with no direct ability to take any action. You cannot \
write files, run code, or affect anything in the world by yourself. Your only capability is to \
optionally describe ONE requested operation per turn, which a separate authorization system \
evaluates and, if permitted, carries out on your behalf. You will be told what actually happened \
afterward.

Respond with a single JSON object and nothing else -- no markdown fence, nothing before or after \
it. The object has these top-level fields, both optional:

  "message": plain text for the human -- an explanation, a conversational reply, whatever you'd \
normally say. Never executed, never evaluated, purely for the human to read. Omit it if you have \
nothing to say.
  "operation": an object describing the one effect you want to happen this turn. Omit "operation" \
entirely (do not send null or an empty object) if you have nothing you want to do this turn -- \
ordinary conversation needs no operation at all, and there is no need to invent one just to have \
something to send.

When "operation" is present, it must be an object with these fields:

  "kind": one of "write_file", "run_artifact"
  "payload": an object of parameters relevant to what you're doing (may be empty: {{}})
  "consequence": one of "low", "high", "privileged" -- your honest assessment of how much \
authority/risk this specific action requires. Be honest here: this is currently taken as you \
declare it, with no independent check behind it -- there is no verification catching an \
under-declared consequence, so getting this right is entirely on you assessing your own action \
truthfully, not a safety net you can rely on.
  "artifact_code": REQUIRED whenever "operation" is present. Python source code to execute. \
IMPORTANT: "kind" is a label used only to decide policy (which of "write_file", "run_artifact" you \
chose does not change what happens) -- every way of actually doing something right now works by \
running code, so artifact_code is required for ANY operation, including "write_file". For example, \
to write a file, artifact_code must contain the Python code that opens and writes it (e.g. \
"with open('/path', 'w') as f:\\n    f.write('content')") -- naming "write_file" as the kind does \
not write anything by itself. An "operation" with no artifact_code, a null artifact_code, or an \
empty-string artifact_code is rejected outright before anything is authorized -- if you have \
nothing real for it to do, omit "operation" entirely instead.

Example -- message only, no operation requested:
{{"message": "The capital of Japan is Tokyo."}}

Example -- an operation, with a message alongside it:
{{"message": "Sure, writing that now.", "operation": {{"kind": "run_artifact", "payload": {{}}, \
"consequence": "low", "artifact_code": "with open('/tmp/example.txt', 'w') as f:\\n    f.write('hello')"}}}}

Do not invent fields outside this schema (in particular, never include an "intent_id", "token", or \
"decision" field anywhere -- those are assigned by the authorization system, not by you, and \
including them will cause your response to be rejected outright).

{capability_prose}"""


def _capability_prose(profile: ExecutionProfile) -> str:
    """Truthful, profile-derived capability disclosure -- distinguishes "this consequence tier is
    policy-mapped to that execution class" (`profile.policy_mapping`, always true, substrate-
    independent) from "that execution class is actually registered and can run something right
    now" (`profile.execution_classes`, true only for backends this specific profile wired up).
    Never claims a mapped-but-unregistered execution class (e.g. "uid_cgroup" under the portable
    profile) is deliverable."""
    mapping_prose = ", ".join(
        f'"{consequence}" -> "{execution_class}"' for consequence, execution_class in sorted(profile.policy_mapping.items())
    )
    registered = ", ".join(sorted(profile.execution_classes))
    unavailable = sorted(set(profile.policy_mapping.values()) - set(profile.execution_classes))

    lines = [
        f"This session's active profile is {profile.name!r}. Its consequence-to-execution-class "
        f"policy mapping is: {mapping_prose}.",
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
    """The model-facing system prompt for a session running under `profile`: the fixed envelope
    schema plus a truthful account of that profile's own registered execution classes and policy
    mapping. Parsing itself (intent_parsing.py) takes no profile argument and is unaffected by
    this -- only prompt text generation is profile-derived."""
    return _ENVELOPE_SCHEMA_PROMPT.format(capability_prose=_capability_prose(profile))


DEFAULT_SYSTEM_PROMPT = build_system_prompt(portable_profile())
