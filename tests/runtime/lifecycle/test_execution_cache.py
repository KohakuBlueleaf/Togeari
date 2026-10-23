"""Fixed binding declarations prevent stale Bound reuse during dynamic program loading."""

import pytest
from kohakunode import NodeLibrary
from kohakunode.sig.model import Library, Port, Signature
from kohakunode.syntax.ast import ANY

from togeari import ConstructionError, ExecutionPolicy, Inline, Machine, Use
from togeari.core.construct.errors import Problems
from togeari.core.program.execution import (
    current_execution,
    note_execution_change,
    validate_bindings,
)
from togeari.testing import SimCall

SOURCE = "@def (x: int)main(y: int):\n    (x)identity(y)\n"


@pytest.fixture
def machines():
    made = []

    def make(**kwargs):
        m = Machine(
            program=SOURCE,
            entry="main",
            bind=kwargs.pop("bind", {"*": Inline()}),
            core="python",
            start_units=False,
            **kwargs,
        )
        made.append(m)
        return m

    yield make
    for m in made:
        m.stop()


def test_identical_load_reuses_bound_region(machines):
    m = machines()
    original = m.code.entries["main"].region
    staged = m._stage(SOURCE, ["main"], Problems())
    assert staged.entries[0][1] is original
    assert original.execution is not None
    assert original.binding_fingerprint == m._binding_fingerprint


@pytest.mark.parametrize("change", ["replace", "add", "revision", "adapter", "policy"])
def test_mutated_binding_fails_before_staging_or_cache_reuse(machines, change):
    use = Use(SimCall(), implementation_revision="host/1")
    m = machines(bind={"*": use})
    original = m.code.entries["main"].region
    before = list(m.code.mem)
    match change:
        case "replace":
            m.bind["*"] = Inline()
        case "add":
            m.bind["other"] = Inline()
        case "revision":
            use.implementation_revision = "host/2"
        case "adapter":
            use.adapter = object()
        case "policy":
            use.policy = ExecutionPolicy(1)
    with pytest.raises(ConstructionError, match="declare all bindings upfront"):
        m._stage(SOURCE, ["main"], Problems())
    assert m.code.entries["main"].region is original
    assert m.code.mem == before


def test_default_adapter_and_protocol_are_fixed(machines):
    m = machines()
    m.defaults.protocol = "different"
    with pytest.raises(ConstructionError, match="bindings changed"):
        validate_bindings(m)


def test_lane_performance_and_observable_changes_are_not_rebinding(machines):
    unit = SimCall()
    m = machines(bind={"*": Use(unit, binding_revision="bundle")})
    lane = m.lanes[0]
    lane.name = "renamed"
    lane.max_inflight = 20
    lane.timeout_s = 4
    lane.retry.max = 5
    validate_bindings(m)
    original = m.code.entries["main"].region
    assert m._stage(SOURCE, ["main"], Problems()).entries[0][1] is original


def test_fixed_binding_metadata_does_not_mutate_original_use(machines):
    use = Inline(implementation_revision="host/1", binding_revision="bundle/1")
    m = machines(bind={"*": use})
    bound = next(m.code.calls(m.code.entries["main"].region)).binding
    assert bound.implementation_revision == use.implementation_revision
    assert bound.binding_revision == use.binding_revision
    assert m.bind["*"] is use


@pytest.mark.parametrize("mode", [None, "legacy", "unknown", True])
def test_persistence_mode_is_explicit_and_validated(machines, mode):
    with pytest.raises(ConstructionError, match="persistence_compatibility"):
        machines(persistence_compatibility=mode)


def test_signature_change_cannot_reuse_stale_decoded_defaults(machines):
    lib = NodeLibrary("cache_signature")

    @lib.node()
    def identity(x: int, unused: int = 1) -> int:
        return x

    m = machines(libraries=[lib])
    original = m.code.entries["main"].region
    signature = lib.signatures["identity"]
    # Replacing the signature contract is not a binding declaration change.
    # The load must decode the new default, not reuse the previous instruction.
    lib.signatures["identity"] = type(signature)(
        signature.name,
        (signature.inputs[0], type(signature.inputs[1])("unused", signature.inputs[1].type, 2)),
        signature.outputs,
    )
    staged = m._stage(SOURCE, ["main"], Problems())
    assert staged.entries[0][1] is not original
    assert staged.entries[0][1].execution["hash"] != original.execution["hash"]


def test_changed_host_function_is_not_hidden_by_a_matching_contract(machines):
    lib = NodeLibrary("replaced_host")

    @lib.node()
    def identity(x: int) -> int:
        return x

    m = machines(libraries=[lib], bind={"*": Inline(implementation_revision="declared/1")})
    original = m.code.entries["main"].region

    def replacement(x):
        return x + 10

    lib.impls["identity"] = replacement
    staged = m._stage(SOURCE, ["main"], Problems())
    other = staged.entries[0][1]
    assert other is not original
    assert next(m.code.calls(original)).binding.fn is identity
    assert next(m.code.calls(other)).binding.fn is replacement
    # The user's revision claim is unchanged: no automatic portable code fingerprint.
    assert other.execution["hash"] == original.execution["hash"]
    [entry] = m._commit(staged)
    assert m.submit({"x": 2}, entry=entry).result(5).outputs == {"y": 12}
    assert m.submit({"x": 2}, entry="main").result(5).outputs == {"y": 2}


class OpaqueDefault:
    def __init__(self, value):
        self.value = value


def opaque_identity(x, unused):
    return unused.value


def test_same_type_unknown_default_is_not_a_cache_identity(machines):
    lib = Library("opaque_cache")
    old, new = OpaqueDefault(1), OpaqueDefault(2)
    lib.add_signature(
        Signature("identity", (Port("x"), Port("unused", ANY, old)), (Port("out"),)),
        opaque_identity,
    )
    m = machines(libraries=[lib])
    region = m.code.entries["main"].region
    lib.signatures["identity"] = Signature(
        "identity", (Port("x"), Port("unused", ANY, new)), (Port("out"),)
    )
    staged = m._stage(SOURCE, ["main"], Problems())
    other = staged.entries[0][1]
    assert other is not region
    assert next(m.code.calls(other)).args[-1][1][1] is new
    assert next(m.code.calls(region)).args[-1][1][1] is old


def test_new_region_cannot_renew_invalidated_bundle(machines):
    unit = SimCall(name="original")
    m = machines(bind={"*": Use(unit, binding_revision="bundle")})
    lane = m.lanes[0]
    replacement = SimCall(name="attached")
    lane.attach(replacement)
    note_execution_change(m, lane, "unit_membership")
    # Even a change back cannot renew the construction attestation.
    lane.detach(replacement, False, lane.clock())
    note_execution_change(m, lane, "unit_membership")
    source = SOURCE.replace("main", "later")
    staged = m._stage(source, ["later"], Problems())
    region = staged.entries[0][1]
    fresh = current_execution(region, m.code, m)
    assert fresh["completeness"]["transitions"] == []
    assert not fresh["completeness"]["complete"]
    assert any("eligible units changed" in x["reason"] for x in fresh["completeness"]["unknown"])
