#!/usr/bin/env python3
"""Interactive REPL driving a real CognitiveLoop against a real Claude model.

The first genuine end-to-end validation of siphonophore-harness: does the loop survive contact
with actual model output, not just ScriptedModel's deterministic text? Run this yourself and type
messages. By default this prints, every turn: the outcome category, intent_id, execution_class,
and (if the model said anything) its conversational text -- pass --verbose to additionally see the
raw completion, useful while examining how the mediation actually behaves rather than just
chatting.

Setup:
    cd /path/to/siphonophore
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=sk-...

Run:
    python3 examples/repl.py --model claude-<a-real-current-model-id>

No model ID is hardcoded here deliberately -- pass the exact, current model id you want to drive
this with.

Uses the "portable" execution profile (siphonophore_harness/composition.py): same_process and
separate_process only -- no uid_cgroup backend registered, so this runs anywhere, no root/Linux
required. artifact_code the model writes runs for real, in this process or a real subprocess,
exactly as the profile's backends do it.

This file is presentation only: it renders the structured DispatchResult/outcome-category surface
siphonophore_harness.outcome and siphonophore_harness.composition already provide. It mints no
Decision, classifies nothing by exception message text, and adds no mediation logic of its own --
see those modules for the actual semantics.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid

from siphonophore_core.authority import Authority
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.composition import ExecutionProfile, portable_profile
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.outcome import DispatchResult, OutcomeCategory, classify_outcome
from siphonophore_harness.prompts import DEFAULT_SYSTEM_PROMPT

try:
    from siphonophore_harness.model_anthropic import AnthropicAPIModel
except ImportError:
    print(
        "error: the `anthropic` package isn't installed. Run: pip install -e \".[anthropic]\"",
        file=sys.stderr,
    )
    raise SystemExit(1)


# Plain-language operator labels for siphonophore_harness.outcome.OutcomeCategory. The category
# itself -- the semantic classification -- always comes from classify_outcome(); this dict only
# supplies the words shown for each already-classified category, never a substitute for it.
_CATEGORY_LABELS: dict[OutcomeCategory, str] = {
    OutcomeCategory.EXECUTED: "executed",
    OutcomeCategory.INPUT_REJECTED: "input rejected",
    OutcomeCategory.AUTHORITY_REJECTED: "authority rejected",
    OutcomeCategory.DENIED: "denied by policy",
    OutcomeCategory.INTEGRITY_REJECTED: "integrity verification failed",
    OutcomeCategory.BACKEND_UNAVAILABLE: "backend unavailable",
    OutcomeCategory.IDENTITY_REJECTED: "execution identity rejected",
    OutcomeCategory.EXECUTION_FAILED: "execution failed",
}


def render_startup_banner(
    profile: ExecutionProfile, *, model_id: str, principal_id: str, authority: Authority | None
) -> str:
    """One-time session-start capability disclosure, derived entirely from ExecutionProfile (plus
    the Authority this session was constructed with, if any) -- no backend objects, no private
    registries, no secrets."""
    policy = ", ".join(f"{consequence}->{execution_class}" for consequence, execution_class in sorted(profile.policy_mapping.items()))
    return "\n".join(
        [
            f"siphonophore-harness REPL -- model={model_id!r}, principal_id={principal_id!r}",
            f"profile: {profile.name}",
            f"execution classes: {', '.join(profile.execution_classes)}",
            f"policy: {policy}",
            render_authority_banner(authority),
        ]
    )


def render_authority_banner(authority: Authority | None) -> str:
    """Safe authority metadata only -- authority_id, allowed kinds, and remaining delegation
    depth. Never the bearer token. Honestly distinguishes "no Authority held" from "one is held"
    rather than implying authority-less is the stronger posture."""
    if authority is None:
        return "authority: none (authority-less session)"
    kinds = ", ".join(sorted(authority.scope.allowed_kinds))
    return (
        f"authority: authority_id={authority.authority_id} scope=[{kinds}] "
        f"remaining_delegation_depth={authority.scope.remaining_delegation_depth}"
    )


def render_turn_result(result: DispatchResult) -> str:
    """Default-visible rendering of a successful dispatch: outcome category, intent_id,
    execution_class, and concise Effect/Decision detail -- no --verbose needed to see any of it."""
    label = _CATEGORY_LABELS[classify_outcome(result)]
    line = f"[{label}] intent_id={result.intent_id} execution_class={result.execution_class}"
    if result.decision.authority_id is not None:
        line += f" authority_id={result.decision.authority_id} order_id={result.decision.order_id}"
    return line + f" detail={result.detail}"


def render_outcome_error(exc: BaseException) -> str:
    """Renders a refused/failed dispatch using classify_outcome() as the sole semantic source --
    never exception message text. Attaches the curated DecisionProjection's safe fields
    (execution_class, authority_id, order_id) when Broker actually minted a Decision before the
    refusal; a pre-Decision refusal (e.g. authority_rejected) renders with no fabricated Decision
    context. An exception classify_outcome() cannot place in the closed category set is shown as an
    explicit unknown/internal state, not silently mislabeled."""
    try:
        category = classify_outcome(exc)
    except ValueError:
        return f"[unknown/internal error] {type(exc).__name__}: {exc}"
    lines = [f"[{_CATEGORY_LABELS[category]}] {exc}"]
    decision = getattr(exc, "decision", None)
    if decision is not None:
        detail = f"  execution_class={decision.execution_class}"
        if decision.authority_id is not None:
            detail += f" authority_id={decision.authority_id} order_id={decision.order_id}"
        lines.append(detail)
    return "\n".join(lines)


def _grant_root_authority(profile: ExecutionProfile, principal_id: str) -> Authority:
    """Constructs this session's Authority using only Gate's existing public granting API
    (issue_order + grant_root_authority), exactly as tests/test_harness_broker.py's own authority
    tests do -- no new issuance mechanism, no delegation, no orchestration. Scoped to the portable
    profile's own default allowed intent kinds; a caller wanting a narrower or delegated Authority
    still has to build it the same way test code does (Gate.issue_order/grant_root_authority/
    delegate), which this helper does not attempt to generalize into a framework."""
    order = profile.gate.issue_order(
        f"repl-order-{uuid.uuid4().hex}",
        f"reference-repl-operator:{principal_id}",
        frozenset(ConsequencePolicy.DEFAULT_ALLOWED_KINDS),
        max_delegation_depth=1,
    )
    return profile.gate.grant_root_authority(order, principal_id=principal_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="exact Anthropic model id, e.g. claude-...")
    parser.add_argument("--api-key", default=None, help="defaults to $ANTHROPIC_API_KEY")
    parser.add_argument("--principal-id", default="human-operator")
    parser.add_argument("--verbose", action="store_true", help="also print the raw model completion")
    parser.add_argument(
        "--grant-root-authority",
        action="store_true",
        help=(
            "run this session holding a freshly granted root Authority (Gate.issue_order + "
            "Gate.grant_root_authority) instead of the ordinary authority-less path"
        ),
    )
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("error: no API key -- pass --api-key or set ANTHROPIC_API_KEY", file=sys.stderr)
        return 1

    model = AnthropicAPIModel(model=args.model, api_key=api_key, system=DEFAULT_SYSTEM_PROMPT)
    profile = portable_profile()

    authority: Authority | None = None
    if args.grant_root_authority:
        authority = _grant_root_authority(profile, args.principal_id)

    loop = CognitiveLoop(model=model, broker=profile.broker, principal_id=args.principal_id, authority=authority)

    print(render_startup_banner(profile, model_id=args.model, principal_id=args.principal_id, authority=authority))
    print("Every message you type becomes a real turn: real model call -> parse_intent -> Gate -> Executor.")
    print("Type 'exit' or Ctrl-D to quit.\n")

    while True:
        try:
            user_message = input("> ")
        except EOFError:
            print()
            break
        if user_message.strip().lower() in ("exit", "quit"):
            break
        if not user_message.strip():
            continue

        try:
            result = loop.step(user_message)
        except Exception as exc:  # noqa: BLE001 -- a REPL should report and keep going, not crash
            print(f"{render_outcome_error(exc)}\n")
            continue

        print(render_turn_result(result))
        if loop.last_message:
            print(f"{loop.last_message}\n")
        else:
            print("[no message this turn]\n")

        if args.verbose:
            raw_completion = loop.history[-2]["content"]
            print(f"[raw completion]\n  {raw_completion}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
