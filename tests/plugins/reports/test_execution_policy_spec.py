"""S1 explanatory policy reports: no units started, sockets or external services."""

import json

import pytest

from togeari import Inline, Lane, Machine, Use
from togeari.core.contracts.execution import ExecutionPolicy
from togeari.core.lanes.health import Retry
from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.plugins.report.spec import binding, machine_spec
from togeari.testing.sim import SimEngine

SOURCE = """@hint timeout_s=7 max_wait_ms=2
(1)neg(a)
@hint timeout_s=9
(a, 2)mul(b)
@hint timeout_s=4
(b, 1)add(c)
(c)to_str(text)
@hint timeout_s=8
(text)len(size)
"""


@pytest.fixture
def machine():
    lane = Lane(
        [SimEngine("call", name="sim")],
        name="work",
        timeout_s=3,
        retry=Retry(max=2, backoff_ms=50),
        lane_dead_s=11,
        max_inflight=4,
    )
    result = Machine(
        program=SOURCE,
        passes=[],
        core="python",
        start_units=False,
        bind={
            "neg": Use(lane, policy=ExecutionPolicy(timeout_s=0)),
            "mul": Use(lane, policy=ExecutionPolicy()),
            "add": Use(lane),
            "to_str": Use(lane),
            "len": Inline(),
        },
    )
    try:
        yield result
    finally:
        result.stop()


def test_report_and_inspector_show_same_resolved_per_call_policy(machine):
    ctx = PluginContext(machine, BasePlugin())
    inspected = ctx.query_now("programs")
    report = machine_spec(ctx)
    calls = report["programs"][0]["calls"]
    assert calls == inspected[0]["calls"]
    assert len(calls) == 5
    by_node = {call["node"]: call for call in calls}
    assert by_node["neg"]["execution_policy"] == {"source": "binding", "timeout_s": 0.0}
    assert by_node["mul"]["execution_policy"] == {"source": "binding", "timeout_s": None}
    assert by_node["add"]["execution_policy"] == {"source": "legacy_hint", "timeout_s": 4.0}
    assert by_node["to_str"]["execution_policy"] == {"source": "none", "timeout_s": None}
    assert by_node["len"]["execution_policy"] == {"source": "none", "timeout_s": None}
    assert by_node["len"]["inline"] is True and by_node["len"]["lane"] is None
    for name in ("neg", "mul", "add", "to_str"):
        assert by_node[name]["lane"] == "work" and by_node[name]["inline"] is False
        assert by_node[name]["pattern"] == name
        assert isinstance(by_node[name]["addr"], int)
        assert by_node[name]["origin"]["module"] == "<text>"
    # Legacy top-level report fields are retained alongside explanatory additions.
    assert report["programs"][0]["semantic_hash"] == machine.code.regions[0].job.semantic_hash
    assert report["programs"][0]["passes"] == []
    assert report["bindings"]["mul"]["policy"] == {"timeout_s": None}
    assert report["bindings"]["add"]["policy"] is None
    assert report["bindings"]["neg"]["policy"] == {"timeout_s": 0.0}
    assert report["bindings"]["len"]["type"] == "Inline"
    assert "policy" not in report["bindings"]["len"]
    assert any("overrides legacy" in warning for warning in report["warnings"])
    assert any("ignored for Inline" in warning for warning in report["warnings"])
    json.dumps(report, allow_nan=False)
    # Neither API exposes the binding's mutable objects through its new rows.
    by_node["neg"]["execution_policy"]["timeout_s"] = 99
    by_node["neg"]["origin"]["module"] = "changed"
    inspected_mul = next(c for c in inspected[0]["calls"] if c["node"] == "mul")
    inspected_mul["execution_policy"]["source"] = "changed"
    fresh = {c["node"]: c for c in ctx.query_now("programs")[0]["calls"]}
    assert fresh["neg"]["execution_policy"]["timeout_s"] == 0.0
    assert fresh["neg"]["origin"]["module"] == "<text>"
    assert fresh["mul"]["execution_policy"]["source"] == "binding"


def test_lane_observable_policy_is_separate_from_advisory_configuration(machine):
    report = machine_spec(PluginContext(machine, BasePlugin()))
    lane = report["lanes"][0]
    assert lane["observable_policy"] == {
        "timeout_s": 3,
        "retry": {"type": "Retry", "max": 2, "backoff_ms": 50},
        "lane_dead_s": 11,
    }
    assert lane["advisory"] == {
        "former": None,
        "select": {"type": "LeastLoaded"},
        "forming": "late",
        "max_inflight": 4,
    }
    for field in ("timeout_s", "retry", "lane_dead_s"):
        assert field not in lane["advisory"]
        assert lane[field] == lane["observable_policy"][field]
    for field in ("former", "select", "forming", "max_inflight"):
        assert lane[field] == lane["advisory"][field]
    lane["observable_policy"]["retry"]["max"] = 999
    assert machine.lanes[0].retry.max == 2
    assert lane["retry"]["max"] == 2


def test_effective_rows_do_not_reinterpret_mutated_binding_configuration(machine):
    # Mutating a Use is not a supported rebind. Reporting must not pretend it is.
    machine.bind["neg"].policy = ExecutionPolicy(99)
    ctx = PluginContext(machine, BasePlugin())
    report = machine_spec(ctx)
    assert report["bindings"]["neg"]["policy"] == {"timeout_s": 99.0}
    effective = next(c for c in report["programs"][0]["calls"] if c["node"] == "neg")
    assert effective["execution_policy"] == {"source": "binding", "timeout_s": 0.0}
    assert report["programs"][0]["calls"] == ctx.query_now("programs")[0]["calls"]


def test_same_node_call_sites_keep_individual_timeouts_and_source(tmp_path):
    source = tmp_path / "policies.kir"
    source.write_text(
        "@hint timeout_s=1\n(1)neg(a)\n@hint timeout_s=2\n(a)neg(b)\n", encoding="utf-8"
    )
    lane = Lane([SimEngine("call", name="sim")], name="work")
    machine = Machine(
        program=source,
        passes=[],
        core="python",
        start_units=False,
        bind={"neg": Use(lane)},
    )
    try:
        ctx = PluginContext(machine, BasePlugin())
        rows = machine_spec(ctx)["programs"][0]["calls"]
        assert rows == ctx.query_now("programs")[0]["calls"]
        assert len(rows) == 2 and len({row["addr"] for row in rows}) == 2
        assert {row["node"] for row in rows} == {"neg"}
        assert {row["execution_policy"]["timeout_s"] for row in rows} == {1.0, 2.0}
        assert {row["execution_policy"]["source"] for row in rows} == {"legacy_hint"}
        assert {row["origin"]["module"] for row in rows} == {"policies.kir"}
        assert {row["origin"]["line"] for row in rows} == {2, 4}
    finally:
        machine.stop()


def test_binding_absent_and_explicit_empty_policy_are_distinct():
    target = object()
    assert binding(Use(target))["policy"] is None
    assert binding(Use(target, policy=ExecutionPolicy()))["policy"] == {"timeout_s": None}
    assert binding(Use(target, policy=ExecutionPolicy(1.5)))["policy"] == {"timeout_s": 1.5}
