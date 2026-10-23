"""Resolve deadline policy once during binding and never reuse hint-stale regions."""

import logging
import math
from types import SimpleNamespace

import pytest

from togeari import ConstructionError, ExecutionPolicy, Inline, Machine, Use
from togeari.core.construct.errors import Problems
from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.program.bind import Defaults, bind_region
from togeari.core.program.code import CodeMemory
from togeari.core.program.job import build_job_programs
from togeari.testing import SimCall
from togeari.testing.sim import SimEngine

SOURCE = "@def (x: int)main(y: int):\n    @hint timeout_s=1\n    (x)identity(y)\n"


def job_for(source=SOURCE):
    problems = Problems()
    jobs = build_job_programs(source, ["main"], [], None, None, problems)
    problems.raise_if_any()
    return jobs[0]


def bind_calls(binding, hint, *, source=SOURCE):
    job = job_for(source)
    code = CodeMemory()
    region = code.load(job)
    calls = list(code.calls(region))
    for call in calls:
        call.hint = dict(hint)
    problems = Problems()
    used = set()
    bind_region(
        code,
        region,
        {"*": binding},
        lambda target: target,
        problems,
        used,
        Defaults(PythonAdapter(), "python"),
    )
    return calls, problems, used


def lane():
    return SimpleNamespace(name="test", units=[])


@pytest.mark.parametrize("timeout", [None, 0, 0.25])
def test_explicit_policy_overrides_valid_or_malformed_legacy_hint(timeout):
    policy = ExecutionPolicy(timeout)
    for legacy in (1, -1, "bad", math.inf, None, {"bad": 1}):
        calls, problems, used = bind_calls(Use(lane(), policy=policy), {"timeout_s": legacy})
        assert problems.items == []
        assert used == {"*"}
        assert len(problems.warnings) == 1 and "overrides" in problems.warnings[0].message
        assert calls[0].binding.policy is policy
        assert calls[0].binding.policy_source == "binding"


def test_absent_policy_and_timeout_resolve_empty_without_warning():
    calls, problems, _ = bind_calls(Use(lane()), {"other": "kept"})
    assert problems.items == problems.warnings == []
    assert calls[0].binding.policy == ExecutionPolicy()
    assert calls[0].binding.policy_source == "none"
    assert calls[0].hint == {"other": "kept"}


@pytest.mark.parametrize("value", [0, 0.25, "0.5", True, False, None])
def test_legacy_timeout_uses_old_float_conversion_then_validation(value):
    calls, problems, _ = bind_calls(Use(lane()), {"timeout_s": value})
    assert problems.items == []
    assert len(problems.warnings) == 1 and "deprecated" in problems.warnings[0].message
    assert calls[0].binding.policy == ExecutionPolicy(None if value is None else float(value))
    assert calls[0].binding.policy_source == "legacy_hint"


@pytest.mark.parametrize(
    "value",
    [-1, "-0.1", "nan", "inf", math.nan, math.inf, "bad", [], {}, 10**5000],
    ids=lambda _: "malformed",
)
def test_malformed_legacy_timeout_is_a_construction_problem(value):
    _, problems, _ = bind_calls(Use(lane()), {"timeout_s": value})
    assert len(problems.items) == 1
    assert "invalid legacy @hint timeout_s" in problems.items[0].message
    assert "main" in problems.items[0].where
    with pytest.raises(ConstructionError, match="invalid legacy"):
        problems.raise_if_any()


@pytest.mark.parametrize("legacy", [-1, "bad", math.nan, {}, True, None])
def test_inline_ignores_legacy_without_validating_it(legacy):
    calls, problems, _ = bind_calls(Inline(), {"timeout_s": legacy})
    assert problems.items == []
    assert len(problems.warnings) == 1 and "ignored for Inline" in problems.warnings[0].message
    assert calls[0].binding.policy == ExecutionPolicy()
    assert calls[0].binding.policy_source == "none"


def test_binding_shortcut_uses_legacy_policy_and_invalid_explicit_type_is_diagnostic():
    calls, problems, _ = bind_calls(lane(), {"timeout_s": 2})
    assert problems.items == [] and calls[0].binding.policy.timeout_s == 2
    for invalid in (True, 1, {}, "bad"):
        _, problems, _ = bind_calls(Use(lane(), policy=invalid), {})
        assert len(problems.items) == 1 and "Use.policy" in problems.items[0].message


def test_binding_snapshots_policy_and_does_not_revisit_instruction_hints():
    binding = Use(lane())
    calls, problems, _ = bind_calls(binding, {"timeout_s": "0.5"})
    assert problems.items == []
    calls[0].hint["timeout_s"] = 100
    binding.policy = ExecutionPolicy(200)
    assert calls[0].binding.policy == ExecutionPolicy(0.5)
    assert calls[0].binding.policy_source == "legacy_hint"


def test_each_call_gets_its_own_legacy_policy_and_binding_diagnostics():
    source = SOURCE + "    @hint timeout_s=2\n    (y)identity(z)\n"
    job = job_for(source)
    code = CodeMemory()
    region = code.load(job)
    problems = Problems()
    bind_region(
        code,
        region,
        {"*": Use(lane())},
        lambda target: target,
        problems,
        set(),
        Defaults(PythonAdapter(), "python"),
    )
    assert problems.items == []
    calls = list(code.calls(region))
    assert sorted(call.binding.policy.timeout_s for call in calls) == [1, 2]
    assert all(call.binding.policy.timeout_s == float(call.hint["timeout_s"]) for call in calls)
    assert len(problems.warnings) == 2


@pytest.mark.parametrize("async_lane", [False, True])
def test_dynamic_load_publishes_migration_warnings_once_after_success(caplog, async_lane):
    binding = Use(SimCall(name="policy-lazy")) if async_lane else Inline()
    machine = Machine(
        program="@def (x: int)initial(y: int):\n    y = x\n",
        entry="initial",
        bind={"identity": binding},
        core="python",
    )
    try:
        count = len(machine.warnings)
        with caplog.at_level(logging.WARNING, logger="togeari.core.construct.machine.loading"):
            names = machine.load(SOURCE, ["main"])
        assert names == ["main"]
        assert len(machine.warnings) == count + 1
        message = machine.warnings[-1].message
        assert ("deprecated" if async_lane else "ignored for Inline") in message
        assert message in caplog.text
        count = len(machine.warnings)
        machine.load(SOURCE, ["main"])
        assert len(machine.warnings) == count
    finally:
        machine.stop()


class FailingStart(SimEngine):
    def start(self):
        raise RuntimeError("policy test start failure")


def test_failed_dynamic_load_does_not_publish_migration_warnings(caplog):
    machine = Machine(
        program="@def (x: int)initial(y: int):\n    y = x\n",
        entry="initial",
        bind={"identity": Use(FailingStart(name="fail-start"))},
        core="python",
    )
    try:
        before = list(machine.warnings)
        with (
            caplog.at_level(logging.WARNING, logger="togeari.core.construct.machine.loading"),
            pytest.raises(ConstructionError, match="policy test start failure"),
        ):
            machine.load(SOURCE, ["main"])
        assert machine.warnings == before
        assert "legacy @hint timeout_s" not in caplog.text
        assert "main" not in machine.code.entries
    finally:
        machine.stop()


def test_cache_requires_full_hash_even_when_source_and_origins_match():
    first = job_for(SOURCE)
    second = job_for(SOURCE.replace("timeout_s=1", "timeout_s=2"))
    assert first.semantic_hash == second.semantic_hash
    assert first.origins == second.origins and first.source == second.source
    assert first.full_hash != second.full_hash
    code = CodeMemory()
    old = code.load(first)
    assert code.cached(first) is old
    assert code.cached(second) is None
    new = code.load(second)
    assert new is not old and new.start >= old.end
    assert list(code.calls(old))[0].hint["timeout_s"] == 1
    assert list(code.calls(new))[0].hint["timeout_s"] == 2
    assert code.cached(second) is new


def test_cache_still_reuses_identical_identity_and_does_not_mix_bindings():
    first = job_for(SOURCE)
    second = job_for(SOURCE.replace("timeout_s=1", "timeout_s=2"))
    code = CodeMemory()
    old = code.load(first)
    new = code.load(second)
    for region in (old, new):
        problems = Problems()
        bind_region(
            code,
            region,
            {"*": Use(lane())},
            lambda target: target,
            problems,
            set(),
            Defaults(PythonAdapter(), "python"),
        )
        problems.raise_if_any()
    assert list(code.calls(old))[0].binding.policy.timeout_s == 1
    assert list(code.calls(new))[0].binding.policy.timeout_s == 2
    assert code.load(job_for(SOURCE.replace("timeout_s=1", "timeout_s=2"))) is new
