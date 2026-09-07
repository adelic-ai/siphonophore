"""EventLog -- a durable, append-oriented, model-neutral JSONL session event stream
(docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md).

The transcript this writes is provenance/audit evidence, NOT the Constructor's workload input (the
WorkOrder, work_order.py, is) -- it exists so a human or a tool like AgentWatch can reconstruct
what actually happened in a session: what the user said, what the model said, which operations were
requested, how the Gate decided, what happened, and how the turn ended.

Writing this file is control-plane/infrastructure behavior, not a model-requested operation --
`CognitiveLoop` (loop.py) never constructs an Intent for its own logging, and this module is never
reached through `Broker.dispatch()`. This is also exactly why the actual file I/O lives HERE and
not in loop.py: `test_harness_structural_proof.py` enforces that loop.py (along with
intent_parsing.py/model.py/broker.py) imports no effect-producing stdlib module, so that the only
way for CognitiveLoop to touch the outside world is through the objects it's explicitly handed
(Model, Broker, and -- as of this module -- an event `sink`, a plain callable with no other
capability). `CognitiveLoop` only ever calls `sink(event_dict)` with an already-built, harness-
authored dict; it never hands the sink anything resembling a path, a file handle, or a capability
of its own. This module is what a caller plugs in as that `sink` when it wants durable JSONL output;
a caller that doesn't want logging at all simply passes `event_sink=None`.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# The reference REPL's own default session-log directory name (examples/repl.py). Defined here,
# not duplicated as a string literal in every file that needs it, so composition.py's
# planning_profile() can exclude it from search_repository's noise filtering without the two files
# drifting out of sync -- a generated session-log transcript is provenance/audit evidence about a
# session, never source/repository evidence a code-investigation search should fan out into.
DEFAULT_SESSION_LOG_DIR_NAME = "siphonophore-sessions"

# Applied to any single free-text field placed into an event -- independent of, and typically much
# smaller than, the model-context bound (loop.py) or the backend capture bound (execution.py):
# this bounds how large a single JSONL *log line* can grow from one piece of content, not how much
# a model sees or how much a backend captures. A WorkOrder's own dict is deliberately exempted --
# see `_redact_and_bound_event`'s docstring for why.
_MAX_EVENT_FIELD_CHARS = 4_000

_FORBIDDEN_EVENT_KEYS = frozenset({
    "token", "api_key", "authority_token", "bearer", "secret", "credential", "credentials",
})


def _bound_text(value: str) -> str:
    if len(value) <= _MAX_EVENT_FIELD_CHARS:
        return value
    return value[:_MAX_EVENT_FIELD_CHARS] + f"...[truncated for event log, {len(value)} characters total]"


def _redact_and_bound_event(event: dict[str, Any]) -> dict[str, Any]:
    """Applied to every event before it is serialized: bounds any free-text string field
    (`_bound_text`) and drops (never silently renames) any key whose name suggests it might carry
    secret/bearer material -- defense in depth alongside the discipline callers of `emit()` are
    already expected to follow (never pass a Decision token, an API key, or raw authority material
    as a field value in the first place). `work_order` payloads (see `finalize`) are exempted from
    per-field truncation: self-containment is the entire point of a WorkOrder, so this module
    accepts them whole rather than mutilating the one artifact this arc explicitly wants durable in
    full -- still passed through the forbidden-key filter, since a WorkOrder's own fields are
    plain, non-secret prose (`work_order.py`'s own schema has no field remotely credential-shaped)."""
    cleaned: dict[str, Any] = {}
    for key, value in event.items():
        if key.lower() in _FORBIDDEN_EVENT_KEYS:
            continue
        if isinstance(value, str) and key != "work_order":
            cleaned[key] = _bound_text(value)
        else:
            cleaned[key] = value
    return cleaned


class EventLog:
    """Owns one session's identity (`session_id`) and, optionally, one JSONL file appended to on
    every `emit()` call. `path=None` means "record nothing" -- `emit()` still returns the
    constructed event (useful for tests and for callers that want in-memory access without a file),
    it just never touches disk.

    Not thread-safe, deliberately: this mirrors `SameProcessBackend`'s own disclosed single-
    dispatch-at-a-time assumption (execution.py) -- nothing in this reference harness calls
    `emit()` from more than one thread at a time, and adding locking for a scenario that does not
    occur would be unjustified complexity, not a real safety improvement."""

    def __init__(self, path: str | Path | None, session_id: str | None = None) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, **fields: Any) -> dict[str, Any]:
        event = _redact_and_bound_event({
            "schema_version": SCHEMA_VERSION,
            "event": event_type,
            "session_id": self.session_id,
            **fields,
        })
        if self._path is not None:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, sort_keys=True) + os.linesep)
        return event

    def sink(self, event: dict[str, Any]) -> None:
        """The plain callable shape `CognitiveLoop(event_sink=...)` expects: `event` is already a
        harness-authored dict with an `event` type plus turn/cycle/correlation fields set by the
        caller (loop.py) -- this only adds the session-scoped fields (`schema_version`,
        `session_id`) and persists it, exactly as `emit()` does for a locally-constructed event.
        Never mutates the dict it was handed."""
        remaining = dict(event)
        event_type = remaining.pop("event")
        self.emit(event_type, **remaining)
