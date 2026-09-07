#!/usr/bin/env python3
"""Interactive REPL driving a real CognitiveLoop against a real Claude model.

The reference V1 cognitive/planning agent (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): an ordinary
conversational interface whose only unusual property is that every real effect it requests is
explicit and mediated. Most turns are pure conversation -- no Intent, no Decision, no trace. When
the model requests a mediated observational operation (--profile planning, the default: read_file/
list_directory/search_repository, read-only, root-confined), the result returns to the model, which
may then answer the original question or request as many further operations as it genuinely needs
-- a real multi-observation task completes in one logical turn, with no small per-turn budget to
ration against (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md's V1.1 addendum). A generous safety net
and a repeated-identical-operation detector exist purely as anomaly backstops, not an ordinary
per-task limit. Once a conversation converges on a fully specified piece of work, the model may
compile a WorkOrder -- a self-contained specification for a future, still-unbuilt Constructor to
realize; compiling one never itself performs work or grants authority.

By default this prints the model's final conversational text, then one compact trace line per
operation actually dispatched this turn (outcome category, intent_id, execution_class); a turn that
requested no operation shows no trace at all. Pass --verbose to additionally see each cycle's raw
completion and model-response diagnostics.

Setup:
    cd /path/to/siphonophore
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=sk-...

Run:
    python3 examples/repl.py --model claude-<a-real-current-model-id>

No model ID is hardcoded here deliberately -- pass the exact, current model id you want to drive
this with.

Profiles (siphonophore_harness/composition.py): "planning" (default) -- read_file/list_directory/
search_repository only, root-confined to --root (default: the current working directory);
structurally no code-execution backend, not merely instructed not to use one. "portable" --
same_process/separate_process (arbitrary Python artifact_code), the original code-execution
reference profile, still available via --profile portable for comparison/testing.

A durable, append-oriented JSONL session event log is written by default (see --session-log/
--no-session-log below) -- provenance/audit evidence, not something the model requests or controls
(siphonophore_harness/session_log.py).

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
from siphonophore_harness.composition import ExecutionProfile, planning_profile, portable_profile
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.outcome import DispatchResult, OperationOutcome, OutcomeCategory, TurnResult, classify_outcome
from siphonophore_harness.prompts import build_system_prompt
from siphonophore_harness.session_log import DEFAULT_SESSION_LOG_DIR_NAME, EventLog

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
    profile: ExecutionProfile, *, model_id: str, principal_id: str, authority: Authority | None,
    session_id: str | None = None, session_log_path: str | None = None,
) -> str:
    """One-time session-start capability disclosure, derived entirely from ExecutionProfile (plus
    the Authority this session was constructed with, if any) -- no backend objects, no private
    registries, no secrets. `allowed_kinds` is shown alongside `execution_classes`/`policy` since
    they are distinct vocabularies for a KindExecutionPolicy-routed profile (composition.py) --
    capability truth applies to the operator's own view exactly as it does to the model's system
    prompt (prompts.py)."""
    policy = ", ".join(f"{consequence}->{execution_class}" for consequence, execution_class in sorted(profile.policy_mapping.items()))
    lines = [
        f"siphonophore-harness REPL -- model={model_id!r}, principal_id={principal_id!r}",
        f"profile: {profile.name}",
        f"allowed operation kinds: {', '.join(sorted(profile.allowed_kinds)) or '(none)'}",
        f"execution classes: {', '.join(profile.execution_classes)}",
        f"policy: {policy}",
        render_authority_banner(authority),
    ]
    if session_log_path is not None:
        lines.append(f"session log: session_id={session_id} path={session_log_path}")
    else:
        lines.append(f"session log: session_id={session_id} DISABLED (--no-session-log)")
    return "\n".join(lines)


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
    text-block filtering, how much text survived that filtering, and (when the SDK response
    provided them) stop_reason/token counts -- small, non-sensitive fields added specifically to
    let an empty-text-block occurrence be diagnosed (was the response truncated by max_tokens?).
    Never shown outside --verbose; never derived from or containing the raw completion, an API key,
    or any other SDK/request internals."""
    line = (
        f"[model response] block_types={list(diagnostics.block_types)} "
        f"text_block_count={diagnostics.text_block_count} "
        f"retained_text_length={diagnostics.retained_text_length}"
    )
    stop_reason = getattr(diagnostics, "stop_reason", None)
    if stop_reason is not None:
        line += f" stop_reason={stop_reason!r}"
    input_tokens = getattr(diagnostics, "input_tokens", None)
    output_tokens = getattr(diagnostics, "output_tokens", None)
    if input_tokens is not None or output_tokens is not None:
        line += f" input_tokens={input_tokens} output_tokens={output_tokens}"
    return line


def render_work_order(work_order: object) -> str:
    """Renders a compiled WorkOrder (work_order.py) -- status, objective, the complete prompt, and
    any non-empty optional fields. Never claims a WorkOrder is execution: it is a structured
    proposal only, and this rendering says so explicitly rather than merely implying it by
    omission."""
    lines = [
        f"  [work_order:{work_order.status}] id={work_order.work_order_id}",
        f"  objective: {work_order.objective}",
        f"  prompt: {work_order.prompt}",
    ]
    for field_name in (
        "requirements", "constraints", "acceptance_criteria", "context", "prohibited_scope",
        "expected_artifacts", "capability_requirements", "data_requirements",
        "network_requirements", "credential_requirements", "isolation_requirements",
        "decomposition_suggestions",
    ):
        values = getattr(work_order, field_name)
        if values:
            lines.append(f"  {field_name}: {list(values)}")
    if work_order.resource_expectations:
        lines.append(f"  resource_expectations: {work_order.resource_expectations}")
    lines.append(
        "  (this WorkOrder grants no authority and launches no workers -- it is a proposal only)"
    )
    return "\n".join(lines)


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
        lines.append(
            f"  [operation safety-net reached] {len(result.operations)} operations attempted this "
            "turn -- this is a runaway backstop, not an ordinary per-task budget"
        )
    if result.loop_detected:
        lines.append(
            f"  [repeated operation detected] {len(result.operations)} operations attempted this "
            "turn before the same request repeated too many times in a row"
        )
    if result.work_order is not None:
        lines.append("")
        lines.append(render_work_order(result.work_order))
    if verbose:
        lines.append("")
        if diagnostics is not None:
            lines.append(render_model_diagnostics(diagnostics))
        lines.append(f"[raw completion]\n  {raw_completion}")
    return "\n".join(lines)


def render_outcome_error(
    exc: BaseException,
    *,
    operations: tuple[OperationOutcome, ...] = (),
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

    `operations` (CognitiveLoop.last_operations -- loop.py's V1.1 truthful-failure-reporting fix)
    is every mediated operation that actually happened THIS turn before it failed, in dispatch
    order -- rendered FIRST, ahead of the error itself, whenever non-empty. A real trial reproduced
    a terminal presentation that read as if the original input had simply been rejected before
    anything happened, when in fact several real operations had already been dispatched and
    mediated earlier in the same failed turn; this makes that impossible to miss rather than
    requiring --verbose or a JSONL inspection to discover.

    Normal mode (verbose=False, the default) is exactly the concise operator error this always
    was -- unchanged. --verbose additionally appends this turn's model-response diagnostics (when
    available) and its raw retained completion (when available), the same evidence a successful
    turn's --verbose output already shows -- so an operator seeing a parse failure can tell, without
    guessing, whether Claude returned non-JSON prose, an empty/non-text response, or something
    else, instead of only ever seeing json.JSONDecodeError's own generic message."""
    lines: list[str] = []
    if operations:
        lines.append(
            f"[{len(operations)} real operation(s) were already dispatched and mediated this turn "
            "before it failed -- the effects below already happened]"
        )
        lines.extend(render_operation_outcome(outcome) for outcome in operations)
        lines.append("")
    try:
        category = classify_outcome(exc)
    except ValueError:
        lines.append(f"[unknown/internal error] {type(exc).__name__}: {exc}")
    else:
        lines.append(f"[{_CATEGORY_LABELS[category]}] {exc}")
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
    tests do -- no new issuance mechanism, no delegation, no orchestration. Scoped to the ACTIVE
    profile's own allowed_kinds (composition.py) -- generic across both "portable" and "planning",
    unlike a hardcoded ConsequencePolicy reference would be; a caller wanting a narrower or
    delegated Authority still has to build it the same way test code does (Gate.issue_order/
    grant_root_authority/delegate), which this helper does not attempt to generalize into a
    framework."""
    order = profile.gate.issue_order(
        f"repl-order-{uuid.uuid4().hex}",
        f"reference-repl-operator:{principal_id}",
        frozenset(profile.allowed_kinds),
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
        "--profile", choices=("planning", "portable"), default="planning",
        help=(
            "\"planning\" (default): read-only observational operations only, no code execution "
            "at all -- the reference cognitive/planning agent. \"portable\": same_process/"
            "separate_process arbitrary-code execution, the original reference profile, kept for "
            "comparison/testing."
        ),
    )
    parser.add_argument(
        "--root", default=None,
        help="root directory the \"planning\" profile's operations are confined to (default: the current working directory)",
    )
    parser.add_argument(
        "--session-log", default=None,
        help="path to write a durable JSONL session event log (default: siphonophore-sessions/<session_id>.jsonl)",
    )
    parser.add_argument(
        "--no-session-log", action="store_true",
        help="disable durable session event logging entirely",
    )
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
    parser.add_argument(
        "--max-operations-per-turn-safety-net", type=int, default=None,
        help=(
            "runaway backstop only, not an ordinary per-task budget -- an anomalously long turn "
            "ends here (default: CognitiveLoop's own default)"
        ),
    )
    parser.add_argument(
        "--repeated-operation-limit", type=int, default=None,
        help=(
            "how many consecutive identical operation requests within one turn are treated as a "
            "mechanical retry loop and refused (default: CognitiveLoop's own default)"
        ),
    )
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("error: no API key -- pass --api-key or set ANTHROPIC_API_KEY", file=sys.stderr)
        return 1

    if args.profile == "planning":
        profile = planning_profile(root=args.root or os.getcwd())
    else:
        profile = portable_profile()
    model = AnthropicAPIModel(model=args.model, api_key=api_key, system=build_system_prompt(profile))

    authority: Authority | None = None
    if args.grant_root_authority:
        authority = _grant_root_authority(profile, args.principal_id)

    session_id = str(uuid.uuid4())
    session_log_path = None if args.no_session_log else (args.session_log or f"{DEFAULT_SESSION_LOG_DIR_NAME}/{session_id}.jsonl")
    event_log = EventLog(path=session_log_path, session_id=session_id)
    event_log.emit(
        "session.started", model=args.model, profile=profile.name, principal_id=args.principal_id,
    )

    loop_kwargs = {}
    if args.max_operations_per_turn_safety_net is not None:
        loop_kwargs["max_operations_per_turn_safety_net"] = args.max_operations_per_turn_safety_net
    if args.repeated_operation_limit is not None:
        loop_kwargs["repeated_operation_limit"] = args.repeated_operation_limit
    loop = CognitiveLoop(
        model=model, broker=profile.broker, principal_id=args.principal_id, authority=authority,
        event_sink=event_log.sink, **loop_kwargs,
    )

    if should_clear_screen(stream=sys.stdout, term=os.environ.get("TERM"), no_clear=args.no_clear):
        clear_screen(sys.stdout)

    print(render_logo())
    print()
    print(render_startup_banner(
        profile, model_id=args.model, principal_id=args.principal_id, authority=authority,
        session_id=session_id, session_log_path=session_log_path,
    ))
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
            print(f"\n{render_outcome_error(exc, operations=loop.last_operations, verbose=args.verbose, raw_completion=error_raw_completion, diagnostics=error_diagnostics)}\n")
            continue

        raw_completion = loop.last_completion if args.verbose else None
        diagnostics = loop.last_diagnostics if args.verbose else None
        print()
        print(render_turn(result, verbose=args.verbose, raw_completion=raw_completion, diagnostics=diagnostics))
        print()

    event_log.emit("session.completed", principal_id=args.principal_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
