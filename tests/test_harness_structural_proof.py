"""DESIGN.md section 7's proof, made structural rather than asserted in prose: the cognitive loop
must be structurally unable to produce an effect except through the Gate. Static analysis over the
harness's own source is the enforcement mechanism -- loop.py, intent_parsing.py, model.py, and
broker.py must import none of a blocklist of effect-producing stdlib modules, so there is no code
path in the chain from "raw completion text" to "Effect" that could touch the outside world other
than through Broker.dispatch() -> Gate.submit() -> Executor.execute()."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from siphonophore_harness import broker, intent_parsing, loop, model

FORBIDDEN_MODULES = {
    "os", "subprocess", "socket", "shutil", "pathlib", "sys", "threading",
    "multiprocessing", "ctypes", "signal", "pty", "fcntl",
}


def _imported_top_level_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


@pytest.mark.parametrize("module", [loop, intent_parsing, model, broker])
def test_module_has_no_effect_producing_imports(module):
    source = Path(module.__file__).read_text()
    imported = _imported_top_level_modules(source)
    forbidden_found = imported & FORBIDDEN_MODULES
    assert forbidden_found == set(), f"{module.__name__} imports effect-producing modules: {forbidden_found}"


def test_cognitive_loop_only_holds_a_model_a_broker_and_an_authority():
    """The class's own __init__ signature is the other half of the structural proof: nothing it
    accepts or stores gives it a second way to reach an effect besides broker.dispatch().

    `authority` (siphonophore_core.authority.Authority) is deliberately allowed here, unlike a
    Gate reference or anything execution-related: it's an inert value object with no methods of
    its own that do anything -- the only thing CognitiveLoop can do with it is hand it to
    broker.dispatch(authority=...), which already independently re-verifies it. Holding one lets
    a loop exercise authority given to it; it does not let the loop derive or grant authority
    itself (that needs a Gate reference, which this signature still excludes), and it does not add
    a second path to an effect."""
    import inspect

    sig = inspect.signature(loop.CognitiveLoop.__init__)
    param_names = set(sig.parameters) - {"self"}
    assert param_names == {"model", "broker", "principal_id", "authority"}
