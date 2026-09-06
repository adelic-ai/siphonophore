#!/usr/bin/env python3
"""Interactive REPL driving a real CognitiveLoop against a real Claude model.

The first genuine end-to-end validation of siphonophore-harness: does the loop survive contact
with actual model output, not just ScriptedModel's deterministic text? Run this yourself and type
messages. One user turn may involve zero or more bounded, independently mediated operations before
the model's final answer (docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md) -- by default this prints
the model's final conversational text, then one compact trace line per operation actually
dispatched this turn (outcome category, intent_id, execution_class); a turn that requested no
operation shows no trace at all. Pass --verbose to additionally see each cycle's raw completion,
useful while examining how the mediation actually behaves rather than just chatting.

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

This file is presentation only: it renders the structured TurnResult/OperationOutcome/
outcome-category surface siphonophore_harness.outcome and siphonophore_harness.composition already
provide. It mints no Decision, classifies nothing by exception message text, and adds no mediation
or continuation logic of its own -- see those modules, and siphonophore_harness.loop, for the
actual semantics.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid

try:
    import readline  # noqa: F401 -- wires stdin history/line-editing into input(); stdlib, POSIX-only
except ImportError:
    readline = None  # some minimal/embedded builds lack it -- input() still works, just without
    # history or arrow-key editing; no custom escape-sequence parsing is added to compensate

from siphonophore_core.authority import Authority
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.composition import ExecutionProfile, portable_profile
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.outcome import DispatchResult, OperationOutcome, OutcomeCategory, TurnResult, classify_outcome
from siphonophore_harness.prompts import build_system_prompt

try:
    from siphonophore_harness.model_anthropic import AnthropicAPIModel
except ImportError:
    print(
        "error: the `anthropic` package isn't installed. Run: pip install -e \".[anthropic]\"",
        file=sys.stderr,
    )
    raise SystemExit(1)


# Stage 4B: fixed session-identity banner -- a module-level constant, not generated art. Presentation
# only; carries no capability/session information of its own (that stays in render_startup_banner()).
_BANNER = (
    "╔══════════════════════════════════════╗\n"
    "║             SIPHONOPHORE             ║\n"
    "║                                      ║\n"
    "║      mediated agent execution        ║\n"
    "╚══════════════════════════════════════╝"
)


def render_logo() -> str:
    """Returns the fixed Siphonophore startup banner. Static text, no dependency on profile/session
    state -- that disclosure stays in render_startup_banner(), printed separately beneath this."""
    return _BANNER


def should_clear_screen(*, stream: object, term: str | None, no_clear: bool) -> bool:
    """True only when clearing is safe and wanted: a genuinely interactive terminal (stream.isatty()
    is True), TERM isn't "dumb", and --no-clear wasn't passed. Any ambiguity (isatty missing/raises,
    non-TTY stream, piped/captured output) resolves to False -- presentation-only, never load-bearing,
    so the safe default is to leave the terminal alone."""
    if no_clear:
        return False
    if term == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def clear_screen(stream: object) -> None:
    """Smallest portable terminal mechanism: ANSI clear-screen + cursor-home, written directly to the
    given stream. No subprocess (`clear`/`cls`), no OS-specific call, no third-party TUI dependency."""
    stream.write("\x1b[2J\x1b[H")
    stream.flush()


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
    """Compact secondary trace footer for a single, already-successful DispatchResult: outcome
    category, execution_class, intent_id, and concise Effect/Decision detail -- no --verbose needed
    to see any of it. Effect.detail is a dict (siphonophore_core.intent.Effect) that is often empty
    on an ordinary successful turn; an empty dict is pure noise here and is omitted, exactly as an
    absent authority_id/order_id already is -- meaningful non-empty detail is still always shown.

    Kept for any caller reaching Broker.dispatch() directly (a DispatchResult is a real, public
    return type independent of CognitiveLoop) -- main()'s own rendering path uses
    render_operation_outcome() below instead, since a CognitiveLoop turn may now carry zero, one,
    or several already-classified OperationOutcome entries rather than a single bare
    DispatchResult (docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md)."""
    label = _CATEGORY_LABELS[classify_outcome(result)]
    line = f"  [{label}] execution_class={result.execution_class} intent_id={result.intent_id}"
    if result.decision.authority_id is not None:
        line += f" authority_id={result.decision.authority_id} order_id={result.decision.order_id}"
    if result.detail:
        line += f" detail={result.detail}"
    return line


def render_operation_outcome(outcome: OperationOutcome) -> str:
    """The multi-cycle counterpart of render_turn_result() above: renders one already-classified
    OperationOutcome (one independently-mediated dispatch cycle of a possibly multi-cycle
    CognitiveLoop turn) in the same compact, one-line-per-operation shape. Reads outcome.category
    directly rather than calling classify_outcome() again -- an OperationOutcome is already the
    result of that classification (siphonophore_harness/loop.py), never re-derived here.

    execution_class is omitted (never shown as a fabricated "None") for authority_rejected, the one
    category with no minted Decision at all. reason -- a short, curated description of why a
    non-executed outcome resulted -- is shown only when there is one; a successful (EXECUTED)
    outcome never carries a reason, matching OperationOutcome's own field semantics."""
    label = _CATEGORY_LABELS[outcome.category]
    parts = [f"  [{label}]"]
    if outcome.execution_class is not None:
        parts.append(f"execution_class={outcome.execution_class}")
    parts.append(f"intent_id={outcome.intent_id}")
    if outcome.authority_id is not None:
        parts.append(f"authority_id={outcome.authority_id} order_id={outcome.order_id}")
    if outcome.detail:
        parts.append(f"detail={outcome.detail}")
    if outcome.reason is not None:
        parts.append(f"reason={outcome.reason}")
    return " ".join(parts)


def render_model_diagnostics(diagnostics: object) -> str:
    """Compact, non-sensitive summary of one AnthropicAPIModel.complete() call's response shape
    (model_anthropic.ModelResponseDiagnostics) -- block types as Anthropic returned them, before
    text-block filtering, plus how much text survived that filtering. Never shown outside
    --verbose; never derived from or containing the raw completion, an API key, or any other
    SDK/request internals."""
    return (
        f"[model response] block_types={list(diagnostics.block_types)} "
        f"text_block_count={diagnostics.text_block_count} "
        f"retained_text_length={diagnostics.retained_text_length}"
    )


def render_turn(
    result: TurnResult,
    *,
    verbose: bool,
    raw_completion: object = None,
    diagnostics: object = None,
) -> str:
    """Composes one turn's full default output: the model's final conversational reply (or an
    explicit no-message placeholder) first, then -- only for a turn that actually requested at
    least one operation -- one compact trace line per independently-mediated cycle, in dispatch
    order (docs/REFERENCE_HARNESS_CONTINUATION_DESIGN.md's Presentation Semantics). A turn that
    never requested an operation (`result.operations == ()`) has nothing truthful to put in a trace
    footer: no execution_class, no intent_id, no "[executed]"/"[...]" label at all -- unchanged
    from the message-only behavior this generalizes.

    `result.message` is deliberately the turn's FINAL message only -- whatever an intermediate
    cycle said before requesting an operation (e.g. "I'll inspect that now") is not shown here by
    default, so it cannot visually compete with the answer; it remains available under --verbose
    via each cycle's own raw completion in history. `result.exhausted` adds one final, distinctly-
    worded line (never an OutcomeCategory label, since it is a harness/session policy decision, not
    a Broker.dispatch() outcome) when the per-turn operation bound ended the turn before a final
    message was produced.

    Verbose raw completion, when requested, stays appended last either way, unchanged from before.
    `diagnostics` (a model_anthropic.ModelResponseDiagnostics, or None when the driving Model
    doesn't provide one, e.g. ScriptedModel) renders just ahead of the raw completion under
    --verbose only; normal-mode output is unaffected either way."""
    lines = [result.message if result.message else "[no message this turn]"]
    if result.operations:
        lines.append("")
        lines.extend(render_operation_outcome(outcome) for outcome in result.operations)
    if result.exhausted:
        lines.append(f"  [operation limit reached] {len(result.operations)} operations attempted this turn")
    if verbose:
        lines.append("")
        if diagnostics is not None:
            lines.append(render_model_diagnostics(diagnostics))
        lines.append(f"[raw completion]\n  {raw_completion}")
    return "\n".join(lines)


def render_outcome_error(
    exc: BaseException,
    *,
    verbose: bool = False,
    raw_completion: object = None,
    diagnostics: object = None,
) -> str:
    """Renders a refused/failed dispatch using classify_outcome() as the sole semantic source --
    never exception message text. Attaches the curated DecisionProjection's safe fields
    (execution_class, authority_id, order_id) when Broker actually minted a Decision before the
    refusal; a pre-Decision refusal (e.g. authority_rejected) renders with no fabricated Decision
    context. An exception classify_outcome() cannot place in the closed category set is shown as an
    explicit unknown/internal state, not silently mislabeled.

    Normal mode (verbose=False, the default) is exactly the concise operator error this always
    was -- unchanged. --verbose additionally appends this turn's model-response diagnostics (when
    available) and its raw retained completion (when available), the same evidence a successful
    turn's --verbose output already shows -- so an operator seeing a parse failure can tell, without
    guessing, whether Claude returned non-JSON prose, an empty/non-text response, or something
    else, instead of only ever seeing json.JSONDecodeError's own generic message."""
    try:
        category = classify_outcome(exc)
    except ValueError:
        lines = [f"[unknown/internal error] {type(exc).__name__}: {exc}"]
    else:
        lines = [f"[{_CATEGORY_LABELS[category]}] {exc}"]
        decision = getattr(exc, "decision", None)
        if decision is not None:
            detail = f"  execution_class={decision.execution_class}"
            if decision.authority_id is not None:
                detail += f" authority_id={decision.authority_id} order_id={decision.order_id}"
            lines.append(detail)
    if verbose:
        lines.append("")
        if diagnostics is not None:
            lines.append(render_model_diagnostics(diagnostics))
        lines.append(f"[raw completion]\n  {raw_completion}")
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
        "--no-clear",
        action="store_true",
        help="do not clear the terminal on interactive startup",
    )
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

    profile = portable_profile()
    model = AnthropicAPIModel(model=args.model, api_key=api_key, system=build_system_prompt(profile))

    authority: Authority | None = None
    if args.grant_root_authority:
        authority = _grant_root_authority(profile, args.principal_id)

    loop = CognitiveLoop(model=model, broker=profile.broker, principal_id=args.principal_id, authority=authority)

    if should_clear_screen(stream=sys.stdout, term=os.environ.get("TERM"), no_clear=args.no_clear):
        clear_screen(sys.stdout)

    print(render_logo())
    print()
    print(render_startup_banner(profile, model_id=args.model, principal_id=args.principal_id, authority=authority))
    print("Every message becomes a model turn. Requested operations are mediated through Gate -> Executor.")
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
            error_raw_completion = loop.last_completion if args.verbose else None
            error_diagnostics = loop.last_diagnostics if args.verbose else None
            print(f"\n{render_outcome_error(exc, verbose=args.verbose, raw_completion=error_raw_completion, diagnostics=error_diagnostics)}\n")
            continue

        raw_completion = loop.last_completion if args.verbose else None
        diagnostics = loop.last_diagnostics if args.verbose else None
        print()
        print(render_turn(result, verbose=args.verbose, raw_completion=raw_completion, diagnostics=diagnostics))
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
