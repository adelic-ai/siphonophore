"""Reusable named execution-profile composition and discoverability
(docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md Stage 3).

`examples/repl.py:62-65` builds exactly one `Gate`/`Executor`/`Broker` wiring, inline, every time:
`Gate(ConsequencePolicy())`, `Broker(gate=gate, executor=Executor(gate))`. That construction is
already fully general -- `Executor(gate, backends=...)` and `ConsequencePolicy(mapping=...)` are
ordinary constructors, sufficient today for every substrate this repository has (portable,
uid_cgroup, k8s_pod -- see `tests/test_harness_loop_linux.py`, `tests/test_harness_loop_k8s_cluster.py`).
This module does not add a new capability to that; it names the one wiring the reference harness
already performs ("portable") so a second front end does not have to copy the four lines by hand,
and gives back a small, read-only view of what was configured, so a caller can answer "what can
this harness execute through?" without reaching into `Executor`/`ConsequencePolicy` internals
(neither of which expose their registered backends/mapping publicly today -- see `execution.py`,
`policy.py`).

This is a composition convenience, not a framework: `compose_profile()` is a plain function
returning a plain, frozen dataclass; there is no registry of profiles, no plugin/discovery
mechanism, and no requirement that any caller use it. Direct construction --
`Gate(ConsequencePolicy(...))`, `Executor(gate, backends={...})`, `Broker(gate=gate,
executor=executor)` -- remains exactly as valid as it was before this module existed; every test
file that constructs a substrate that way today (`test_harness_loop_k8s_cluster.py`,
`test_harness_loop_linux.py`) is untouched by this module and needs no change to keep working.

Discoverability without core changes: `Executor` and `ConsequencePolicy` do not publicly expose
their registered backends/active mapping (`execution.py`'s `_backends`, `policy.py`'s `_mapping`
are both private, with no getter). Rather than widen core to answer a reference-harness UX
question, `ExecutionProfile` retains a curated copy of exactly what `compose_profile()` itself was
asked to configure, at the moment it configures it -- the harness knows what it built, so core
does not need to grow introspection it has no other reason to have. `execution_classes` is a
tuple of names only (never a backend instance -- nothing here lets a caller reach a backend object
merely to learn its name); `policy_mapping` is an immutable view over a private copy (mutating the
returned mapping cannot affect the `ConsequencePolicy` `compose_profile()` built).

Composition, not orchestration: `ExecutionProfile` carries `Gate`, `Executor`, and `Broker`
because a caller demonstrating authority/delegation needs the `Gate` (`issue_order`/
`grant_root_authority`/`delegate`, per `docs/REFERENCE_HARNESS_IMPLEMENTATION_PLAN.md` Stage 5's
own recipe) alongside the `Broker` it dispatches through. It owns no `CognitiveLoop`, no `Model`,
no session state, and no opinion about *when* to grant authority -- that is unchanged, existing
`siphonophore_harness`/`siphonophore_core` responsibility, not something this module adds to.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from siphonophore_core.execution import Executor, ExecutionBackend, SameProcessBackend, SeparateProcessBackend
from siphonophore_core.execution_readonly import ListDirectoryBackend, ReadFileBackend, SearchRepositoryBackend
from siphonophore_core.mediation import Gate
from siphonophore_core.policy import ConsequencePolicy, KindExecutionPolicy

from .broker import Broker


@dataclass(frozen=True)
class ExecutionProfile:
    """A named `Gate`/`Executor`/`Broker` triple, plus a curated, read-only view of what
    `compose_profile()` configured them with. Not a new capability -- `gate`, `executor`, and
    `broker` are the exact same public core/harness types a caller could construct directly;
    `execution_classes`/`policy_mapping` are a convenience projection of values the caller of
    `compose_profile()` already supplied, not derived by inspecting `executor`/`gate` internals.

    `name` is a plain label (e.g. `"portable"`) -- it carries no behavior of its own and is never
    used to branch inside this module; a caller is free to construct as many differently-named
    profiles as it wants, including substrates this module has never heard of.

    `allowed_kinds` (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md) is the set of `Intent.kind` values
    this profile's `Policy` will ever permit -- distinct from `execution_classes`, which names
    registered *backends*. For a `ConsequencePolicy`-based profile (`compose_profile()`) the two
    vocabularies are unrelated (kinds like `"run_artifact"` map to execution classes like
    `"same_process"` via a declared consequence tier); for a `KindExecutionPolicy`-based profile
    (`compose_kind_profile()`) they coincide by construction. Exists so a capability-truthful
    prompt (`prompts.py`) can describe which OPERATIONS a session can request, not merely which
    backends are registered underneath them."""

    name: str
    gate: Gate
    executor: Executor
    broker: Broker
    execution_classes: tuple[str, ...]
    policy_mapping: Mapping[str, str]
    allowed_kinds: tuple[str, ...] = ()


def compose_profile(
    name: str,
    *,
    backends: dict[str, ExecutionBackend],
    policy_mapping: dict[str, str],
    allowed_kinds: tuple[str, ...] | None = None,
) -> ExecutionProfile:
    """Generic, substrate-neutral composition: build a fresh `Gate`/`Executor`/`Broker` from an
    explicit backend registry and consequence-mapping, and return them alongside a curated,
    read-only view of what was configured.

    Substrate-neutral by construction: this function knows nothing about `same_process`,
    `k8s_pod`, `uid_cgroup`, or any other execution-class string -- it only ever sees whatever
    `backends`/`policy_mapping` the caller passes. A future substrate (a container tier, a VM
    tier, anything implementing `ExecutionBackend`) uses this exact function, unchanged, by
    supplying its own `backends`/`policy_mapping` -- see `portable_profile()` below for the one
    concrete example this module ships.

    `backends`/`policy_mapping` are copied, not stored by reference, before being handed to
    `Executor`/`ConsequencePolicy` and before being retained for `execution_classes`/
    `policy_mapping` -- mutating the dict a caller passed in after this call returns has no effect
    on the returned `ExecutionProfile` or the `Executor`/`ConsequencePolicy` it wraps.
    """
    effective_allowed_kinds = allowed_kinds if allowed_kinds is not None else ConsequencePolicy.DEFAULT_ALLOWED_KINDS
    policy = ConsequencePolicy(mapping=dict(policy_mapping), allowed_kinds=allowed_kinds)
    gate = Gate(policy)
    executor = Executor(gate, backends=dict(backends))
    broker = Broker(gate=gate, executor=executor)
    return ExecutionProfile(
        name=name,
        gate=gate,
        executor=executor,
        broker=broker,
        execution_classes=tuple(sorted(backends.keys())),
        policy_mapping=MappingProxyType(dict(policy_mapping)),
        allowed_kinds=tuple(effective_allowed_kinds),
    )


def portable_profile() -> ExecutionProfile:
    """The named `"portable"` profile: today's ordinary reference-harness configuration --
    `same_process` and `separate_process` backends only, `ConsequencePolicy`'s default
    consequence-to-execution-class mapping. Wires exactly what `examples/repl.py:62-65`
    constructs inline today (`Gate(ConsequencePolicy())`, `Broker(gate=gate,
    executor=Executor(gate))`), as a reusable, documented convenience.

    Does not wire: `uid_cgroup`/`uid_cgroup_checkin` (needs real root on real Linux, see
    `execution_uid_cgroup.py`) or `k8s_pod` (needs a reachable cluster, see `execution_k8s.py`).
    Registering either remains exactly as available as before this function existed --
    `compose_profile()` above, or direct `Executor(gate, backends={...})`/`ConsequencePolicy(
    mapping={...})` construction, exactly as `tests/test_harness_loop_linux.py` and
    `tests/test_harness_loop_k8s_cluster.py` already do.
    """
    return compose_profile(
        "portable",
        backends={
            "same_process": SameProcessBackend(),
            "separate_process": SeparateProcessBackend(),
        },
        policy_mapping=ConsequencePolicy.DEFAULT_MAPPING,
    )


def compose_kind_profile(
    name: str,
    *,
    backends: dict[str, ExecutionBackend],
    kind_mapping: dict[str, str] | None = None,
) -> ExecutionProfile:
    """The `KindExecutionPolicy`-routed counterpart to `compose_profile()` above
    (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): builds a `Gate`/`Executor`/`Broker` where
    `execution_class` is chosen directly by `intent.kind` (`policy.KindExecutionPolicy`), not
    derived from a declared consequence tier. Needed for typed-operation profiles (e.g.
    `planning_profile()` below) where a `"low"`/`"high"`/`"privileged"` trust-tier declaration has
    no meaning -- an operation's execution class simply IS what it is.

    `kind_mapping` defaults to the identity mapping over `backends`' own keys (`{k: k for k in
    backends}`) -- the common case where an operation's kind and its execution class are the same
    string, exactly how `planning_profile()` uses it. `ExecutionProfile.policy_mapping` holds this
    kind -> execution_class mapping (the same `{str: str}` shape `compose_profile()` already
    populates it with for a consequence-routed profile) -- a caller reading `profile.policy_mapping`
    for capability disclosure (`prompts.py`) works unmodified regardless of which composition
    function built the profile."""
    if kind_mapping is None:
        kind_mapping = {kind: kind for kind in backends}
    policy = KindExecutionPolicy(dict(kind_mapping))
    gate = Gate(policy)
    executor = Executor(gate, backends=dict(backends))
    broker = Broker(gate=gate, executor=executor)
    return ExecutionProfile(
        name=name,
        gate=gate,
        executor=executor,
        broker=broker,
        execution_classes=tuple(sorted(backends.keys())),
        policy_mapping=MappingProxyType(dict(kind_mapping)),
        allowed_kinds=tuple(sorted(kind_mapping)),
    )


def planning_profile(root: str) -> ExecutionProfile:
    """The reference "planning"/cognitive-agent profile
    (docs/REFERENCE_HARNESS_V1_ARCHITECTURE.md): read-only, root-confined observational operations
    only -- `read_file`, `list_directory`, `search_repository` (`execution_readonly.py`) -- and
    structurally NO `run_artifact`/`write_file`/any code-execution backend at all.

    This is the enforcement half of "the reference cognitive agent does not itself edit the
    repository": it is not merely instructed not to via a system prompt. The active `Executor` has
    no backend registered for either code-bearing kind, and `KindExecutionPolicy` denies both kinds
    outright regardless (they are simply absent from its mapping) -- `Gate.submit()` returns
    `permitted=False` for either before `Executor` is even reached, and even if it somehow weren't,
    there is no backend to dispatch to. Two independent refusal layers, neither of which depends on
    the model choosing to cooperate.

    `root` confines every `read_file`/`list_directory`/`search_repository` operation this session
    can ever issue -- typically the repository root the agent is meant to investigate. Confinement
    is real (`execution_readonly.py`'s own `_confine`, symlink-aware), not a naming convention;
    nothing above this profile's own construction can widen it -- a completion cannot supply its
    own root, only a path relative to the one this function was called with."""
    backends: dict[str, ExecutionBackend] = {
        "read_file": ReadFileBackend(root=root),
        "list_directory": ListDirectoryBackend(root=root),
        "search_repository": SearchRepositoryBackend(root=root),
    }
    return compose_kind_profile("planning", backends=backends)
