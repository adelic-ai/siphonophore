"""AnthropicAPIModel -- a real, network-backed Model (model.py's Model ABC) using Anthropic's own
official Python SDK against the raw Messages API, API-key billed.

Deliberately NOT subscription/OAuth-billed (the Claude Agent SDK path): that SDK wraps the actual
Claude Code CLI, a full local agent runtime with real tool-execution capability. Using it as
`Model.complete()`'s implementation would mean the "just get me a completion" call is backed by
something that -- unless every tool is correctly disabled, a configuration choice that has to stay
correct forever, not a structural fact -- could perform a real local effect entirely underneath
`Model.complete()`, before any Intent exists, invisible to the Gate. A raw API client is
structurally incapable of that: it is a function that sends bytes out and gets bytes back, nothing
else. That is a materially stronger guarantee for the one piece of this architecture
(`model.py`/this file) that sits outside the Gate's mediation boundary by design and outside
test_harness_structural_proof.py's static-analysis reach. Costs metered API dollars instead of
subscription quota -- a deliberate tradeoff, not an oversight.

Deliberately kept OUT of model.py itself, in its own file: model.py (with loop.py,
intent_parsing.py, broker.py) is covered by test_harness_structural_proof.py's check that none of
those files import an effect-producing stdlib module. Fetching a completion legitimately needs
network access -- it is not an "effect" DESIGN.md's Gate governs, only what gets parsed into an
Intent and dispatched afterward is -- but keeping that network-capable code in its own,
clearly-named file keeps model.py's own guarantee ("nothing here needs anything but text in, text
out") checkable by the same static analysis, rather than adding an exception inside the file that
check protects.

Requires the `anthropic` package -- Anthropic's own official Python SDK, a mature, first-party
dependency, justified per DESIGN.md section 0 (a specific reason a hand-rolled HTTP client would be
worse, from a mature source, and the section 0 dependency bar is met by neither being Strands nor
this project's own discarded architecture). Install with `pip install -e ".[anthropic]"`; nothing
in `siphonophore_harness/__init__.py` imports this module, so importing the harness package itself
never requires having `anthropic` installed.

Known limitation, not yet solved: CognitiveLoop.history uses a third role, "effect" (loop.py),
which this module maps to Anthropic's "user" role (a delegation's effect is naturally the next
thing "reported to" the model, the same shape a tool result would take). That mapping assumes
strict user/assistant/user/assistant alternation in the underlying history. If a `step()` call
raises before appending its assistant/effect turns (a parse failure or a refused dispatch --
loop.py's own documented behavior), the *next* step() call's freshly-appended user turn lands
immediately after the previous, unresolved user turn, breaking that alternation -- the Messages API
requires strict alternation and will reject a request shaped that way. Not handled here; a caller
driving retries after a failed step() needs to account for it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import anthropic

from .model import Model


@dataclass(frozen=True)
class ModelResponseDiagnostics:
    """Structural facts about one Anthropic response, captured before `complete()` filters its
    content blocks down to the retained text it returns. Exists so that a downstream parse
    failure -- which happens after `complete()` has already returned -- doesn't erase the only
    evidence of what the response actually contained: block_types is recorded from
    `response.content` before any filtering, not reconstructed afterward from the (possibly empty)
    retained text.

    `stop_reason` and the two token counts (`input_tokens`/`output_tokens`) exist specifically to
    let a real empty-text-block occurrence (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md's defect 1 --
    a real trial produced block_types=['thinking','text'] with retained_text_length=0) be
    diagnosed empirically: a `stop_reason` of `"max_tokens"` alongside a small `max_tokens` budget
    would directly confirm the leading hypothesis (the response was truncated before any real text
    was emitted) rather than leaving it as an open question forever. Token counts are small,
    non-sensitive integers -- not billing/cost data, not a repr of `response.usage` itself.

    Deliberately excludes everything else about the SDK response object: no id, no headers, no repr
    of `response` itself, and nothing from the request (no API key, no messages). Fields absent
    from a given SDK response (e.g. a fake/test double that doesn't set `usage`) are recorded as
    `None`, never fabricated."""

    block_types: tuple[str, ...]
    text_block_count: int
    total_block_count: int
    retained_text_length: int
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class AnthropicAPIModel(Model):
    """Sends `messages` to a real Claude model over Anthropic's Messages API and returns the text
    of its response. Requires an API key -- ANTHROPIC_API_KEY by default, or passed explicitly.

    `last_diagnostics` (a `ModelResponseDiagnostics`) reflects the most recent `complete()` call --
    `None` until the first call is made. It is diagnostic-only display state: nothing downstream of
    `complete()`'s return value reads it, and it never influences what text gets returned or how it
    is parsed."""

    def __init__(self, model: str, api_key: str | None = None, system: str | None = None,
                 max_tokens: int = 4096) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("AnthropicAPIModel requires an API key -- pass api_key= or set ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model
        self._system = system
        self._max_tokens = max_tokens
        self.last_diagnostics: ModelResponseDiagnostics | None = None

    def complete(self, messages: list[dict]) -> str:
        anthropic_messages = [
            {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
            for m in messages
        ]
        kwargs: dict = {}
        if self._system is not None:
            kwargs["system"] = self._system
        response = self._client.messages.create(
            model=self._model, max_tokens=self._max_tokens, messages=anthropic_messages, **kwargs
        )
        block_types = tuple(block.type for block in response.content)
        text = "".join(block.text for block in response.content if block.type == "text")
        usage = getattr(response, "usage", None)
        self.last_diagnostics = ModelResponseDiagnostics(
            block_types=block_types,
            text_block_count=sum(1 for block_type in block_types if block_type == "text"),
            total_block_count=len(block_types),
            retained_text_length=len(text),
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        return text
