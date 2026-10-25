"""Knob specs agree with the knob checks: every knob has a spec, the values a spec allows pass
``checked`` and the values it rules out fail it; the ``knob_specs`` query answers each knob's
value and the contributed policies by the roles they fill."""

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.core.construct.knobs import KNOB_DEFAULTS, KNOB_SPECS, POLICY_KNOBS, checked
from togeari.core.plugin.base import BasePlugin
from togeari.policies import Fifo, LeastLoaded, SizeOrWait, TargetLoad
from togeari.testing import SimBatch

POLICIES = {"admit": TargetLoad(None, waiting=4), "fifo": Fifo()}
LIB = NodeLibrary("knob-specs")


@LIB.node()
def f(x: int) -> int:
    return x


def allowed(spec: dict) -> list:
    """Values the spec allows."""
    out = [None] if spec.get("nullable") else []
    lo = spec.get("min", 0)
    match spec["kind"]:
        case "int":
            out += [lo, lo + 1, lo + 1000]
        case "number":
            out += [lo, lo + 0.5, 1e9, float("inf")]
        case "rate":
            out += [0, 0.5, 1]
        case "bool":
            out += [True, False]
        case "choice":
            out += list(spec["choices"])
        case "ints":
            out += [[1], [1, 0], [5, 0, 7]]
        case "policy":
            out += [{"admission": "admit", "issue": "fifo"}[spec["role"]]]
    return out


def ruled_out(spec: dict) -> list:
    """Values the spec rules out."""
    out = [] if spec.get("nullable") else [None]
    lo = spec.get("min", 0)
    match spec["kind"]:
        case "int":
            out += [lo - 1, lo + 0.5, True, "3"]
        case "number":
            out += [lo - 1, True, "3"]
        case "rate":
            out += [-0.1, 1.5, True]
        case "bool":
            out += [0, 1, "true"]
        case "choice":
            out += ["nonsense", 3]
        case "ints":
            out += [[], [0], [1, -1], [1, 2, 3, 4], [1.5]]
        case "policy":
            out += ["no-such-policy", 3]
    return out


def test_every_knob_has_a_spec():
    assert set(KNOB_SPECS) == set(KNOB_DEFAULTS) | set(POLICY_KNOBS)


@pytest.mark.parametrize("name", sorted(KNOB_SPECS))
def test_the_spec_agrees_with_the_check(name):
    spec = KNOB_SPECS[name]
    for v in allowed(spec):
        assert checked(name, v, POLICIES)[1] is None, (name, v)
    for v in ruled_out(spec):
        assert checked(name, v, POLICIES)[1] is not None, (name, v)


class Contributes(BasePlugin):
    name = "contributes"

    def contribute_policies(self) -> dict:
        return {
            "tight": TargetLoad(None, waiting=2),
            "small": SizeOrWait(2, 1),
            "ll": LeastLoaded(),
        }


def test_the_knob_specs_query():
    lane = Lane([SimBatch(name="b0", latency_ms=1, max_batch=4)], former=SizeOrWait(4, 1), name="b")
    m = Machine(
        program="@def (x: int)main(y):\n    (x)f(y)\n",
        entry="main",
        libraries=[LIB],
        bind={"f": Use(lane), "kir.std.*": Inline()},
        plugins=[Contributes()],
    )
    with m:
        got = m.inspect("knob_specs").result(10)
        lanes = m.inspect("lanes").result(10)
    assert set(got["knobs"]) == set(KNOB_SPECS)
    assert got["knobs"]["window"] == {"kind": "int", "min": 1, "value": 64}
    assert got["knobs"]["admission"]["kind"] == "policy"
    assert got["policies"]["admission"] == ["tight"]
    assert got["policies"]["former"] == ["small"]
    assert got["policies"]["select"] == ["ll"]
    assert lanes[0]["former"] == "SizeOrWait" and lanes[0]["select"]
