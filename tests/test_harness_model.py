from __future__ import annotations

import pytest

from siphonophore_harness.model import ScriptedModel


def test_scripted_model_returns_completions_in_order():
    model = ScriptedModel(["first", "second"])
    assert model.complete([]) == "first"
    assert model.complete([]) == "second"


def test_scripted_model_raises_once_exhausted():
    model = ScriptedModel(["only"])
    model.complete([])
    with pytest.raises(RuntimeError):
        model.complete([])
