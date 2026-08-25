"""siphonophore-core -- SDK for mediated, attributable agent harnesses.

See DESIGN.md at the repo root for the architecture. No dependency on any agent-framework SDK
(section 0). Contains no Agent, Model, Prompt, Conversation, or reasoning loop -- those belong only
in a harness built from this SDK (section 6), never in core.

This package consolidates what lab/001-009 independently proved -- each experiment necessarily
reimplemented its own copy of Intent/Decision/Gate/Executor, since lab scripts are required to be
self-contained (DESIGN.md section 0; HISTORY.md's no-dependencies incident). This is the first
place those proven pieces become one real, importable, tested package rather than nine separate
copies. HISTORY.md's own account of that incident is why this package does NOT import anything
from lab/ either -- built fresh here, informed by what lab/ proved, not by importing its code.
"""
from __future__ import annotations
