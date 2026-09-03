"""Acceptance check for the architectural warning test the k8s design review named: 'if the
general Siphonophore core suddenly needs to understand things named Pod, Job, Namespace,
ServiceAccount, etc., stop and determine whether Kubernetes-specific concepts have leaked above
the substrate boundary.'

Portable, no cluster needed -- pure source scan. Every file in siphonophore_core/ except
execution_k8s.py (the one module allowed to know what a Pod is) must stay free of
Kubernetes-specific vocabulary. Two layers:

1. A prose/reference scan (regex over the raw source): `Namespace`/`Job`/`Pod`/`ServiceAccount`
   only when capitalized as a standalone word (Python's own `exec()` namespace and a docstring's
   plain-English "job" are lowercase and correctly don't match); `Kubernetes`/`kubectl`/`k8s`
   case-insensitively, since those have no legitimate unrelated meaning in this codebase. Catches
   docstring/comment/class-name-shaped leaks.

2. An identifier scan (AST): dataclass field names and function/method parameter names, checked
   case-insensitively against a Kubernetes-noun list, regardless of capitalization. An adversarial
   review of the first version of this file (regex-only, capitalized-only) confirmed it would
   silently pass the most realistic leak vector -- a field like `namespace: str` or `pod_id: str`
   added to Intent/Effect/Decision/Authority/Order/Scope -- because that's exactly the lowercase
   shape a real Python field/parameter takes, indistinguishable by the old regex from
   execution.py's own unrelated local variable `namespace: dict = {...}` (a local var inside a
   method body, not a class-level field or a parameter -- which is why layer 2 only walks
   dataclass field declarations and function signatures, not arbitrary local variables)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "siphonophore_core"
ALLOWED_K8S_FILE = "execution_k8s.py"

_CAPITALIZED_K8S_NOUNS = re.compile(r"\b(Pod|Job|Namespace|ServiceAccount)\b")
_UNAMBIGUOUS_K8S_TERMS = re.compile(r"kubernetes|kubectl|k8s", re.IGNORECASE)

# Case-insensitive, exact-identifier match (not substring) against dataclass field names and
# function/method parameter names -- the realistic leak vector layer 1 can't see.
_K8S_IDENTIFIER_NOUNS = {
    "namespace", "pod", "pod_name", "pod_id", "job", "job_name", "container", "containers",
    "deployment", "cluster", "kubeconfig", "serviceaccount", "service_account",
    "replicaset", "statefulset", "daemonset",
}


def _core_files_excluding_k8s_backend() -> list[Path]:
    return sorted(p for p in CORE_DIR.glob("*.py") if p.name != ALLOWED_K8S_FILE)


def _leaking_identifiers(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.target.id.lower() in _K8S_IDENTIFIER_NOUNS:
                        hits.append(f"field {stmt.target.id!r} in class {node.name!r}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.arg.lower() in _K8S_IDENTIFIER_NOUNS:
                    hits.append(f"parameter {arg.arg!r} in function {node.name!r}")
    return hits


def test_core_files_exist_to_scan():
    files = _core_files_excluding_k8s_backend()
    assert len(files) >= 10, f"expected the usual core module set, found {len(files)}: {files}"


def test_no_kubernetes_vocabulary_leaks_into_substrate_neutral_core():
    offenders: dict[str, list[str]] = {}
    for path in _core_files_excluding_k8s_backend():
        text = path.read_text()
        hits = _CAPITALIZED_K8S_NOUNS.findall(text) + _UNAMBIGUOUS_K8S_TERMS.findall(text)
        hits += _leaking_identifiers(ast.parse(text, filename=str(path)))
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        f"Kubernetes-specific vocabulary leaked into substrate-neutral core files: {offenders} -- "
        f"this belongs only in {ALLOWED_K8S_FILE}"
    )


def test_identifier_scan_actually_catches_a_lowercase_field_leak():
    """Regression guard for the gap the adversarial review found: without this layer, a field
    literally named `namespace` or `pod_id` added to a core dataclass passed silently."""
    sample = "from dataclasses import dataclass\n\n@dataclass\nclass Fake:\n    namespace: str\n    pod_id: str\n"
    hits = _leaking_identifiers(ast.parse(sample))
    assert any("namespace" in h for h in hits)
    assert any("pod_id" in h for h in hits)
