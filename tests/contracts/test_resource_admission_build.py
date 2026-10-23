"""Machine construction rejects resource admission typos before any unit starts."""

import pytest

from togeari import ConstructionError, Lane, Machine, Use
from togeari.policies import All, BudgetFormer, Fixed, Greedy, ResourcePressure
from togeari.testing import SimEngine


def estimate(item):
    return {"bytes": 1}


def lane(former=None):
    unit = SimEngine("batch", name="unit")
    return Lane([unit], name="work", former=former or BudgetFormer({"bytes": 10}, estimate))


def build(work, admission):
    return Machine(
        program="(1)identity(out)",
        bind={"*": Use(work)},
        admission=admission,
        start_units=False,
        core="python",
    )


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing_lane", "missing"),
        ("nonbudget", "BudgetFormer"),
        ("missing_dimension", "unestimated"),
    ],
)
def test_resource_admission_bad_configuration_is_construction_error(case, expected):
    work = lane(Greedy() if case == "nonbudget" else None)
    name = "typo" if case == "missing_lane" else "work"
    dimensions = {"tokens": 10} if case == "missing_dimension" else {"bytes": 10}
    with pytest.raises(ConstructionError, match=expected) as caught:
        build(work, ResourcePressure(name, dimensions))
    assert any(problem.where == "admission" for problem in caught.value.problems)
    assert work.units[0].clock is None


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("by_name", [False, True])
def test_resource_admission_matching_configuration_and_nested_all_build(nested, by_name):
    work = lane()
    pressure = ResourcePressure("work" if by_name else work, {"bytes": 20})
    rule = All(Fixed(10), All(pressure)) if nested else pressure
    machine = build(work, rule)
    try:
        assert machine.knobs["admission"] is rule
        assert not machine.units_started
        assert work.units[0].clock is None
    finally:
        machine.stop()


def test_custom_admission_policy_is_not_introspected_or_called_at_construction():
    class Custom:
        @property
        def rules(self):
            raise AssertionError("custom policy must not be introspected")

        def allows(self, pool, signals):
            raise AssertionError("construction must not run admission")

    rule = Custom()
    machine = build(lane(Greedy()), rule)
    try:
        assert machine.knobs["admission"] is rule
        assert not machine.units_started
    finally:
        machine.stop()
