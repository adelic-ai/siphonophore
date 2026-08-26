#!/usr/bin/env python3
"""Interactive REPL driving a real CognitiveLoop against a real Claude model.

The first genuine end-to-end validation of siphonophore-harness: does the loop survive contact
with actual model output, not just ScriptedModel's deterministic text? Run this yourself, type
messages, and watch what actually happens on each turn -- the raw completion, then the resulting
Effect (or the error, if the model's output didn't satisfy parse_intent or the Gate refused it).

Setup:
    cd /path/to/siphonophore
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=sk-...

Run:
    python3 examples/repl.py --model claude-<a-real-current-model-id>

No model ID is hardcoded here deliberately -- pass the exact, current model id you want to drive
this with.

Uses only the portable execution tiers (same_process, separate_process) -- no uid_cgroup backend
registered, so this runs anywhere, no root/Linux required. artifact_code the model writes runs for
real, in this process or a real subprocess, exactly as Executor's default backends do it.
"""
from __future__ import annotations

import argparse
import os
import sys

from siphonophore_core.execution import Executor
from siphonophore_core.mediation import Gate, GateViolation
from siphonophore_core.policy import ConsequencePolicy
from siphonophore_harness.broker import Broker
from siphonophore_harness.intent_parsing import IntentParseError
from siphonophore_harness.loop import CognitiveLoop
from siphonophore_harness.prompts import DEFAULT_SYSTEM_PROMPT

try:
    from siphonophore_harness.model_anthropic import AnthropicAPIModel
except ImportError:
    print(
        "error: the `anthropic` package isn't installed. Run: pip install -e \".[anthropic]\"",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="exact Anthropic model id, e.g. claude-...")
    parser.add_argument("--api-key", default=None, help="defaults to $ANTHROPIC_API_KEY")
    parser.add_argument("--principal-id", default="human-operator")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("error: no API key -- pass --api-key or set ANTHROPIC_API_KEY", file=sys.stderr)
        return 1

    model = AnthropicAPIModel(model=args.model, api_key=api_key, system=DEFAULT_SYSTEM_PROMPT)
    gate = Gate(ConsequencePolicy())
    broker = Broker(gate=gate, executor=Executor(gate))
    loop = CognitiveLoop(model=model, broker=broker, principal_id=args.principal_id)

    print(f"siphonophore-harness REPL -- model={args.model!r}, principal_id={args.principal_id!r}")
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
            effect = loop.step(user_message)
        except IntentParseError as exc:
            print(f"[parse error -- the model's completion did not satisfy the intent schema]\n  {exc}\n")
            continue
        except GateViolation as exc:
            print(f"[refused by the Gate/Executor]\n  {exc}\n")
            continue
        except Exception as exc:  # noqa: BLE001 -- a REPL should report and keep going, not crash
            print(f"[unexpected error: {type(exc).__name__}: {exc}]\n")
            continue

        raw_completion = loop.history[-2]["content"]
        print(f"[model said]\n  {raw_completion}\n")
        print(f"[effect]\n  execution_class={effect.execution_class!r}\n  detail={effect.detail}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
