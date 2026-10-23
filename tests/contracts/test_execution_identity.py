"""Execution declarations are not inferred implementation or determinism fingerprints."""

import copy
import json
import logging
import math
from dataclasses import FrozenInstanceError, replace

import pytest
from kohakunode import NodeLibrary
from kohakunode.sig.model import Library, Port, Signature
from kohakunode.syntax.ast import ANY

from togeari import ConstructionError, ExecutionPolicy, Inline, Lane, Machine, Retry, Use
from togeari.core.program import execution
from togeari.core.program.execution import (
    build_execution,
    canonical_bytes,
    compatibility,
    current_execution,
    execution_context,
    layout_hash,
    note_execution_change,
    observe_execution,
    reuse_decision,
    warn_unverified,
)
from togeari.core.runtime.backend.persistence import result_execution
from togeari.testing import SimCall

LIB = NodeLibrary("identity_tests")


@LIB.node()
def host(x: int) -> int:
    return x + 1


SOURCE = "@def (x: int)main(y: int):\n    (x)host(y)\n"
CONTROL = """@def (xs: list)main(out):
    (xs)collect(x, i, `body`, y -> out)
    body:
        @try:
            (x, i)add(y)
        @except error:
            y = 0
"""


@pytest.fixture
def machines():
    made = []

    def make(source=SOURCE, binding=None, **kwargs):
        m = Machine(
            program=source,
            entry="main",
            libraries=kwargs.pop("libraries", [LIB]),
            bind=kwargs.pop("bind", {"*": binding if binding is not None else Inline()}),
            core=kwargs.pop("core", "python"),
            start_units=False,
            **kwargs,
        )
        made.append(m)
        return m

    yield make
    for m in made:
        m.stop()


def manifest(machine):
    return current_execution(machine.code.entries["main"].region, machine.code, machine)


def test_unknown_is_explicit_and_ordinary_execution_is_quiet(machines, caplog):
    m = machines()
    current = manifest(m)
    call = current["identity"]["calls"][0]
    assert call["implementation_revision"] is call["binding_revision"] is None
    assert current["completeness"]["unknown"]
    assert compatibility(current, current, "strict")["allow"] is False
    assert compatibility(current, current, "warn")["status"] == "unverified"
    with caplog.at_level(logging.WARNING, logger="togeari.persistence"):
        assert m.submit({"x": 2}).result(5).outputs == {"y": 3}
    assert "unverified" not in caplog.text


def test_host_and_bundle_revisions_are_independent(machines):
    unit = SimCall(name="one")
    host_only = manifest(machines(binding=Use(unit, implementation_revision="host/1")))
    assert not host_only["completeness"]["complete"]
    assert any("engines" in x["path"] for x in host_only["completeness"]["unknown"])
    first = manifest(
        machines(binding=Use(unit, implementation_revision="host/1", binding_revision="bundle/1"))
    )
    assert first["completeness"]["complete"]
    for host_revision, bundle_revision in (("host/2", "bundle/1"), ("host/1", "bundle/2")):
        other = manifest(
            machines(
                binding=Use(
                    unit, implementation_revision=host_revision, binding_revision=bundle_revision
                )
            )
        )
        assert compatibility(first, other, "warn")["status"] == "incompatible"
    inline = manifest(machines(binding=Inline(implementation_revision="host/1")))
    assert inline["completeness"]["complete"]


@pytest.mark.parametrize("field", ["implementation_revision", "binding_revision"])
@pytest.mark.parametrize("value", ["", 3, True, {}, []])
def test_revision_validation_is_construction_diagnostic(machines, field, value):
    with pytest.raises(ConstructionError, match=field):
        machines(binding=Inline(**{field: value}))


def test_bound_identity_is_copied_and_frozen(machines):
    use = Inline(implementation_revision="one", binding_revision="whole")
    m = machines(binding=use)
    bound = next(m.code.calls(m.code.entries["main"].region)).binding
    use.implementation_revision = "two"
    assert bound.implementation_revision == "one"
    with pytest.raises(FrozenInstanceError):
        bound.binding_revision = "other"


def test_canonical_bytes_and_policy_provenance(machines):
    source = SOURCE.replace("    (x)", "    @hint timeout_s=1\n    (x)")
    a = machines(source, Use(SimCall(name="a"), binding_revision="bundle"))
    b = machines(
        source, Use(SimCall(name="b"), policy=ExecutionPolicy(1), binding_revision="bundle")
    )
    c = machines(
        SOURCE, Use(SimCall(name="c"), policy=ExecutionPolicy(1), binding_revision="bundle")
    )
    assert manifest(a)["hash"] == manifest(b)["hash"] == manifest(c)["hash"]
    assert canonical_bytes({"b": 2, "a": 1}) == canonical_bytes({"a": 1, "b": 2})
    assert json.loads(canonical_bytes(manifest(a))) == manifest(a)


def test_performance_config_and_source_origins_do_not_identify_execution(machines):
    one = Lane([SimCall(name="one", capacity=1)], name="first", max_inflight=2)
    many = Lane(
        [SimCall(name="a", capacity=50), SimCall(name="b", capacity=50)],
        name="second",
        max_inflight=200,
    )
    a = machines(binding=Use(one, binding_revision="bundle"), window=1)
    b = machines("\n\n" + SOURCE, Use(many, binding_revision="bundle"), window=100, mutates="wait")
    assert a.code.entries["main"].region.job.origins != b.code.entries["main"].region.job.origins
    assert manifest(a)["hash"] == manifest(b)["hash"]


@pytest.mark.parametrize(
    "field,value", [("timeout_s", 0.5), ("lane_dead_s", 5), ("retry", Retry(9, 10))]
)
def test_observable_lane_policy_changes_and_change_back(machines, field, value):
    lane = Lane([SimCall()], timeout_s=1)
    m = machines(binding=Use(lane, binding_revision="bundle"))
    region = m.code.entries["main"].region
    original = observe_execution(region, m.code, m)
    old = getattr(lane, field)
    setattr(lane, field, value)
    note_execution_change(m, lane, "policy_changed")
    changed = observe_execution(region, m.code, m)
    assert changed["manifest"]["hash"] != original["manifest"]["hash"]
    assert changed["manifest"]["completeness"]["complete"]
    assert changed["epoch"] == original["epoch"] + 1
    setattr(lane, field, old)
    note_execution_change(m, lane, "policy_changed")
    restored = manifest(m)
    assert restored["hash"] == original["manifest"]["hash"]
    active = execution_context(region, m.code, m, original["epoch"])
    assert active["completeness"]["transitions"]
    for mode in ("warn", "strict"):
        assert not compatibility(active, active, mode)["allow"]
    assert compatibility(restored, restored, "strict")["allow"]


def test_membership_invalidates_old_bundle_but_new_job_has_no_mixed_history(machines):
    lane = Lane([SimCall(name="one")])
    m = machines(binding=Use(lane, binding_revision="bundle"))
    old = manifest(m)
    lane.attach(SimCall(name="two"))
    note_execution_change(m, lane, "unit_membership")
    fresh = manifest(m)
    assert fresh["hash"] == old["hash"]  # Replica count is not semantic identity.
    assert fresh["completeness"]["transitions"] == []
    assert not fresh["completeness"]["complete"]
    assert any("eligible units changed" in x["reason"] for x in fresh["completeness"]["unknown"])
    assert compatibility(fresh, fresh, "warn")["allow"]
    assert not compatibility(fresh, fresh, "strict")["allow"]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_layout_normalizes_try_regions_collect_fields_and_earlier_loads(machines, core):
    a = machines(CONTROL, core=core)
    earlier = "@def ()other(x):\n    x = 2\n"
    b = machines(CONTROL, programs={earlier: ["other"]}, core=core)
    first = a.code.entries["main"].region
    shifted = b.code.entries["main"].region
    assert first.start != shifted.start and first.index != shifted.index
    assert first.tries and shifted.tries
    assert layout_hash(first, a.code) == layout_hash(shifted, b.code)
    assert manifest(a)["hash"] == manifest(b)["hash"]
    each = next(ins for ins in b.code.mem[shifted.start : shifted.end] if ins.collect_target)
    original = layout_hash(shifted, b.code)
    each.collect_target = "different"
    assert layout_hash(shifted, b.code) != original


class Opaque:
    def __repr__(self):
        raise AssertionError("identity must not repr host defaults")


class Unpicklable(Opaque):
    def __reduce__(self):
        raise AssertionError("identity must not pickle host defaults")

    def __togeari_digest__(self):
        raise AssertionError("identity must not trust arbitrary digest hooks")


def default_node(value):
    return 7


@pytest.mark.parametrize("default", [Opaque(), Unpicklable()], ids=["opaque", "unpicklable"])
@pytest.mark.parametrize("core", ["python", "rust"])
def test_opaque_defaults_execute_but_are_not_certified(machines, default, core):
    lib = Library("opaque_defaults")
    lib.add_signature(
        Signature("opaque", (Port("value", ANY, default),), (Port("out"),)), default_node
    )
    m = machines(
        "@def ()main(out):\n    ()opaque(out)\n",
        Inline(binding_revision="bundle"),
        libraries=[lib],
        core=core,
        persistence_compatibility="strict",
    )
    current = manifest(m)
    assert any("default" in x["path"] for x in current["completeness"]["unknown"])
    assert not compatibility(current, current, "strict")["allow"]
    assert m.submit({}).result(5).outputs == {"out": 7}


@pytest.mark.parametrize(
    "value",
    [math.inf, -math.inf, math.nan, 1 << 20000],
    ids=["inf", "negative-inf", "nan", "huge-int"],
)
def test_portable_scalar_defaults_have_json_safe_identity(machines, value):
    lib = Library("scalar_defaults")
    lib.add_signature(
        Signature("scalar", (Port("value", ANY, value),), (Port("out"),)), default_node
    )
    m = machines(
        "@def ()main(out):\n    ()scalar(out)\n", Inline(binding_revision="bundle"), libraries=[lib]
    )
    current = manifest(m)
    assert current["completeness"]["complete"]
    assert json.loads(canonical_bytes(current)) == current
    assert m.submit({}).result(5).outputs == {"out": 7}


def test_signature_default_and_effect_changes_affect_manifest(machines):
    lib = Library("signature_changes")
    base = Signature("scalar", (Port("value", ANY, 1),), (Port("out"),))
    lib.add_signature(base, default_node)
    source = "@def ()main(out):\n    ()scalar(out)\n"
    a = machines(source, Inline(binding_revision="bundle"), libraries=[lib])
    original = manifest(a)
    lib.signatures["scalar"] = replace(base, inputs=(Port("value", ANY, 2),), ordered=True)
    b = machines(source, Inline(binding_revision="bundle"), libraries=[lib])
    assert manifest(b)["hash"] != original["hash"]


@pytest.mark.parametrize("container", [list, tuple])
def test_shared_default_containers_are_not_certified_as_distinct_values(machines, container):
    shared = []
    lib = Library("aliased_defaults")
    value = container([shared, shared])
    lib.add_signature(
        Signature("aliased", (Port("value", ANY, value),), (Port("out"),)), default_node
    )
    m = machines(
        "@def ()main(out):\n    ()aliased(out)\n",
        Inline(binding_revision="bundle"),
        libraries=[lib],
    )
    current = manifest(m)
    assert any("shared or cyclic" in x["reason"] for x in current["completeness"]["unknown"])
    assert not compatibility(current, current, "strict")["allow"]
    assert m.submit({}).result(5).outputs == {"out": 7}


def test_invalid_manifest_and_warning_deduplication(machines, caplog):
    m = machines()
    current = manifest(m)
    for bad in (None, {}, {"schema": "togeari.execution", "version": 999}):
        assert not compatibility(bad, current, "warn")["allow"]
    corrupt = copy.deepcopy(current)
    corrupt["hash"] = "wrong"
    assert compatibility(corrupt, current, "warn")["status"] == "invalid"
    decision = compatibility(current, current, "warn")
    with caplog.at_level(logging.WARNING, logger="togeari.persistence"):
        warn_unverified(m, decision, execution_hash=current["hash"])
        count = len(caplog.records)
        warn_unverified(m, decision, execution_hash=current["hash"])
    assert len(caplog.records) == count == len(decision["issues"])


def test_draining_is_ineligible_but_temporary_down_does_not_change_identity(machines):
    unit = SimCall(name="one")
    lane = Lane([unit])
    m = machines(binding=Use(lane, binding_revision="bundle"))
    original = manifest(m)
    lane.state[id(unit)].state = "down"
    note_execution_change(m, lane, "health")
    assert manifest(m) == original
    lane.state[id(unit)].state = "draining"
    note_execution_change(m, lane, "unit_membership")
    drained = manifest(m)
    assert unit in lane.units  # Still held until its outstanding calls settle.
    assert drained["epoch"] == original["epoch"] + 1
    assert drained["identity"]["calls"][0]["engines"] == []
    assert not drained["completeness"]["complete"]
    lane.state[id(unit)].state = "detached"
    note_execution_change(m, lane, "unit_membership")
    assert manifest(m)["epoch"] == drained["epoch"]


def test_equivalent_policy_number_types_do_not_change_identity(machines):
    lane = Lane([SimCall()], timeout_s=1)
    m = machines(binding=Use(lane, binding_revision="bundle"))
    old = manifest(m)
    lane.timeout_s = 1.0
    assert manifest(m) == old


def test_module_relocation_is_stable_but_dependency_content_changes(machines, tmp_path):
    main = '@import "module.kir" as algo\n@def (x: int)main(y: int):\n    (x)algo.run(y)\n'
    module = "@export run\n@def (x: int)run(y: int):\n    (x, 1)add(y)\n"
    manifests = []
    for folder in ("first", "relocated", "changed"):
        directory = tmp_path / folder
        directory.mkdir()
        path = directory / "main.kir"
        path.write_text(main, encoding="utf-8")
        (directory / "module.kir").write_text(
            module.replace("x, 1", "x, 2") if folder == "changed" else module,
            encoding="utf-8",
        )
        manifests.append(manifest(machines(path)))
    assert manifests[0]["hash"] == manifests[1]["hash"]
    assert manifests[0]["identity"]["program"]["dependency_hash"]
    assert (
        manifests[2]["identity"]["program"]["dependency_hash"]
        != manifests[0]["identity"]["program"]["dependency_hash"]
    )
    assert not compatibility(manifests[0], manifests[2], "warn")["allow"]


def test_lane_adoption_does_not_change_manifest_across_cores(machines):
    python = machines(binding=Use(SimCall(name="py"), binding_revision="bundle"), core="python")
    rust = machines(binding=Use(SimCall(name="rs"), binding_revision="bundle"), core="rust")
    assert manifest(python) == manifest(rust)


def test_live_identity_and_reuse_decisions_cache_static_work(machines, monkeypatch):
    counts = {name: 0 for name in ("_layout", "_contracts", "_hash")}

    def counter(name, function):
        def wrapped(*args, **kwargs):
            counts[name] += 1
            return function(*args, **kwargs)

        return wrapped

    for name in counts:
        monkeypatch.setattr(execution, name, counter(name, getattr(execution, name)))
    source = "@def (x: int)main(y: int):\n" + "    (x)host(x)\n" * 100 + "    y = x\n"
    lane = Lane([SimCall()], timeout_s=1)
    m = machines(source, Use(lane, binding_revision="bundle"))
    region = m.code.entries["main"].region
    first = current_execution(region, m.code, m)
    decision = reuse_decision(region, m.code, m)
    assert counts["_layout"] == counts["_contracts"] == 1
    stats = {"execution": copy.deepcopy(first), "execution_epoch": first["epoch"]}
    assert result_execution(region, m.code, m, stats) == first["hash"]
    warmed = dict(counts)
    for _ in range(100):
        assert current_execution(region, m.code, m) is first
        assert reuse_decision(region, m.code, m) is decision
        assert result_execution(region, m.code, m, stats) == first["hash"]
    assert counts == warmed

    # Only dynamic identity is rendered again, not code/signature normalization.
    lane.timeout_s = 2
    changed = current_execution(region, m.code, m)
    assert changed["hash"] != first["hash"]
    assert counts["_layout"] == counts["_contracts"] == 1
    assert reuse_decision(region, m.code, m) is not decision
    assert result_execution(region, m.code, m, stats) is None
    fresh_stats = {"execution": copy.deepcopy(changed), "execution_epoch": changed["epoch"]}
    assert result_execution(region, m.code, m, fresh_stats) == changed["hash"]
    warmed = dict(counts)
    for _ in range(100):
        reuse_decision(region, m.code, m)
        assert result_execution(region, m.code, m, stats) is None
        assert result_execution(region, m.code, m, fresh_stats) == changed["hash"]
    assert counts == warmed

    # Explicit report copies cannot mutate the cached expected identity.
    report = build_execution(region, m.code, m)
    report["identity"]["layout"]["hash"] = "tampered"
    assert current_execution(region, m.code, m)["identity"]["layout"]["hash"] != "tampered"


def test_job_history_overlay_cannot_poison_fresh_cached_manifest(machines):
    lane = Lane([SimCall()], timeout_s=1)
    m = machines(binding=Use(lane, binding_revision="bundle"))
    region = m.code.entries["main"].region
    before = current_execution(region, m.code, m)
    lane.timeout_s = 2
    active = execution_context(region, m.code, m, before["epoch"])
    fresh = current_execution(region, m.code, m)
    assert active["completeness"]["transitions"]
    assert not fresh["completeness"]["transitions"]
    assert fresh["completeness"]["complete"]


def test_epoch_does_not_affect_portable_compatibility(machines):
    m = machines(binding=Inline(implementation_revision="host/1"))
    original = manifest(m)
    other = copy.deepcopy(original)
    other["epoch"] = 900
    assert compatibility(original, other, "strict")["allow"]
    assert build_execution(m.code.entries["main"].region, m.code, m)["hash"] == original["hash"]
