"""Determinism is explicit eligibility, not inferred from absence of mutation/effects."""

from dataclasses import FrozenInstanceError

import pytest
from kohakunode import NodeLibrary
from kohakunode.records import RecordOperation
from kohakunode.sig.env import Resolved
from kohakunode.sig.model import Library, Port, Signature
from kohakunode.sig.records import RecordField, RecordSchema
from kohakunode.sig.std import STD
from kohakunode.syntax.ast import TypeExpr

from togeari import ConstructionError, Inline, Machine, Use
from togeari.core.contracts.reuse import builtin_revision
from togeari.core.program.execution import current_execution, validate_bindings
from togeari.stores import MemoryStore
from togeari.testing import SimCall

LIB = NodeLibrary("reuse_tests")


@LIB.node()
def sample(x: int) -> int:
    return x


@LIB.node(ordered=True)
def effect(x: int) -> int:
    return x


@LIB.node(mutates=["x"])
def mutate(x: list) -> list:
    x.append(1)
    return x


@pytest.fixture
def machines():
    made = []

    def make(binding=None, node="sample", **kwargs):
        source = f"@def (x)main(y):\n    (x){node}(y)\n"
        m = Machine(
            program=source,
            entry="main",
            libraries=kwargs.pop("libraries", [LIB]),
            bind={"*": Inline() if binding is None else binding},
            store=MemoryStore(),
            core="python",
            start_units=False,
            **kwargs,
        )
        made.append(m)
        return m

    yield make
    for m in made:
        m.stop()


def call(machine):
    region = machine.code.entries["main"].region
    return next(machine.code.calls(region))


def manifest(machine):
    return current_execution(machine.code.entries["main"].region, machine.code, machine)


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize(
    "declared,expected,source",
    [(None, False, "none"), (False, False, "binding"), (True, True, "binding")],
)
def test_host_declaration_resolution(machines, inline, declared, expected, source):
    binding = Inline(deterministic=declared) if inline else Use(SimCall(), deterministic=declared)
    m = machines(binding)
    assert call(m).binding.deterministic is expected
    assert call(m).binding.reuse_source == source
    assert manifest(m)["identity"]["calls"][0]["reuse"] == {
        "content": expected,
        "squash": expected,
        "positional": True,
    }


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("bad", [0, 1, "true", [], {}])
def test_non_bool_declarations_fail_construction(machines, inline, bad):
    binding = Inline(deterministic=bad) if inline else Use(SimCall(), deterministic=bad)
    with pytest.raises(ConstructionError, match="deterministic must be a bool or None"):
        machines(binding)


def test_legacy_explicit_content_is_warned_and_equivalent_to_explicit_assertion(machines):
    legacy = machines(Use(SimCall(), store="content", binding_revision="bundle"))
    explicit = machines(
        Use(SimCall(), store="content", deterministic=True, binding_revision="bundle")
    )
    assert call(legacy).binding.deterministic is True
    assert call(legacy).binding.reuse_source == "legacy_content"
    assert any("legacy determinism assertion" in w.message for w in legacy.warnings)
    assert not any("legacy determinism assertion" in w.message for w in explicit.warnings)
    assert manifest(legacy)["hash"] == manifest(explicit)["hash"]
    assert "reuse_source" not in manifest(legacy)["identity"]["calls"][0]


def test_false_content_is_a_conflict_even_for_builtin_or_ordered_calls(machines):
    for node in ("sample", "effect", "identity"):
        with pytest.raises(ConstructionError, match="conflicts with deterministic=False"):
            machines(Use(SimCall(), store="content", deterministic=False), node=node)


@pytest.mark.parametrize(
    "declared,expected,source",
    [(None, True, "builtin"), (False, False, "binding"), (True, True, "binding")],
)
def test_exact_standard_builtin_can_be_disabled(machines, declared, expected, source):
    m = machines(Inline(deterministic=declared), node="identity")
    assert call(m).binding.deterministic is expected
    assert call(m).binding.reuse_source == source


@pytest.mark.parametrize("declared,expected", [(None, True), (False, False), (True, True)])
def test_generated_collect_helpers_honor_explicit_override(declared, expected):
    source = (
        "@def (xs: list)main(out):\n"
        "    (xs)collect(x, `body`, x -> out)\n    body:\n        x = x\n"
    )
    m = Machine(
        program=source,
        entry="main",
        passes=["expand_collect", "order_dataflow", "inline", "strip_meta"],
        bind={"*": Inline(deterministic=declared)},
        core="python",
        start_units=False,
    )
    try:
        helpers = [
            ins
            for ins in m.code.calls(m.code.entries["main"].region)
            if ins.node in ("kir.collect_items", "kir.collect_append")
        ]
        assert helpers
        assert all(ins.binding.inline and ins.binding.deterministic is expected for ins in helpers)
    finally:
        m.stop()


def test_name_shadow_and_replaced_function_are_not_recognized_builtins(machines, monkeypatch):
    lib = Library("kir.std")
    lib.add_signature(Signature("identity", (Port("x"),), (Port("y"),)), sample)
    shadow = machines(node="identity", libraries=[lib])
    assert call(shadow).binding.deterministic is False
    monkeypatch.setitem(STD.impls, "identity", sample)
    replaced = machines(node="identity")
    assert call(replaced).binding.deterministic is False


def test_exact_record_operation_is_recognized_but_subclass_is_not():
    schema = RecordSchema(
        "Point", (RecordField("x", TypeExpr("name", "int")),), schema_id="test.Point"
    )
    exact = RecordOperation(schema, "make")

    class OtherOperation(RecordOperation):
        pass

    assert builtin_revision(None, exact) is not None
    assert builtin_revision(None, OtherOperation(schema, "make")) is None
    assert builtin_revision(Resolved(STD.signatures["identity"], STD), sample) is None


@pytest.mark.parametrize("node", ["effect", "mutate"])
def test_effects_block_content_and_squash_regardless_declaration(machines, node):
    m = machines(Use(SimCall(), store="content", deterministic=True), node=node)
    assert call(m).binding.deterministic is True
    assert call(m).binding.store is None
    reuse = manifest(m)["identity"]["calls"][0]["reuse"]
    assert reuse["content"] is False and reuse["squash"] is False
    assert any("ignored" in w.message for w in m.warnings)


@pytest.mark.parametrize("deterministic", [None, False, True])
def test_mutating_positional_cache_is_ignored_without_mutation_outcome_envelope(
    machines, deterministic
):
    m = machines(Use(SimCall(), store="positional", deterministic=deterministic), node="mutate")
    assert call(m).binding.store is None
    assert manifest(m)["identity"]["calls"][0]["reuse"] == {
        "content": False,
        "squash": False,
        "positional": False,
    }
    assert any("mutating" in w.message and "positional" in w.message for w in m.warnings)
    ordered = machines(Use(SimCall(), store="positional", deterministic=True), node="effect")
    assert call(ordered).binding.store is None
    assert manifest(ordered)["identity"]["calls"][0]["reuse"]["positional"] is False


def test_revision_is_not_determinism_and_determinism_changes_execution_identity(machines):
    disabled = machines(Inline(implementation_revision="host/1", binding_revision="bundle"))
    enabled = machines(
        Inline(implementation_revision="host/1", binding_revision="bundle", deterministic=True)
    )
    assert call(disabled).binding.deterministic is False
    assert manifest(disabled)["hash"] != manifest(enabled)["hash"]


def test_deterministic_declaration_is_frozen_and_in_binding_mutation_guard(machines):
    binding = Inline(deterministic=True)
    m = machines(binding)
    bound = call(m).binding
    with pytest.raises(FrozenInstanceError):
        bound.deterministic = False
    binding.deterministic = False
    assert bound.deterministic is True
    with pytest.raises(ConstructionError, match="bindings changed"):
        validate_bindings(m)
