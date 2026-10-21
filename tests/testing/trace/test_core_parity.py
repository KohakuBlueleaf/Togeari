"""Trace parity of the two cores (spec/11 §11.5): every generated program under every
configuration, and every example's program, gives the same decision log, the same records, the
same cycle count, the same virtual time, the same dispatcher counters and the same lane state on
the Rust core as on the Python core.

Around that suite: other trace seeds, lane subclasses, policies changed on the instance, lane
settings and policies changed mid-run, units taken down, detached and attached, pause, cancel and
stop, snapshots and stores over two runs, the path with no listener on the bus, the memory each
core lets go of, and the native digest.
"""

import gc
import importlib.util
import math
import random
import re
import statistics
import sys
import threading
import time
import types
import weakref
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

import pytest
from kohakunode.testing import entry_program
from trace_machines import CAPACITY, LANE_OF, generated

from togeari import Inline, Lane, Machine, Retry, Use
from togeari.core.construct.core import rust_available
from togeari.core.construct.errors import ConstructionError
from togeari.core.construct.knobs import checked
from togeari.core.contracts.digest import digest_with_weak
from togeari.core.contracts.io import SourceItem
from togeari.core.contracts.results import UnitError
from togeari.core.lanes.native import NativeLane
from togeari.policies import (
    Affinity,
    All,
    DeepestFirst,
    Fifo,
    Fixed,
    Greedy,
    HintPriority,
    LaneAware,
    LeastLoaded,
    MemoryBound,
    RoundRobin,
    SizeOrWait,
    TargetLoad,
    Throughput,
)
from togeari.stores.memory import MemoryStore
from togeari.testing.trace import ScriptedEngine, first_difference, run_trace

try:
    from togeari import _rs
except ImportError:
    _rs = None

pytestmark = pytest.mark.skipif(not rust_available(), reason="togeari._rs has no dispatcher")

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
GEN_SEEDS = range(16)
CONFIGS = {
    "default": {},
    "deepest_first": {"issue": DeepestFirst()},
    "fifo": {"issue": Fifo()},
    "lane_aware": {"issue": LaneAware()},
    "hint_priority": {"issue": HintPriority()},
    "no_run_ahead": {"run_ahead": False},
    "small_window": {"window": 3, "retire_buffer": 2},
    "mutates_wait": {"mutates": "wait"},
    "copy_always": {"mutates": "copy-always"},
    "no_reuse": {"squash_reuse": 0},
    "not_recorded": {"record": None},
    "debug_checks": {"assert_alias": True, "fingerprint_inputs": True},
    "admission_mix": {"admission": All(Fixed(4), MemoryBound(1 << 30), TargetLoad("call", 64))},
    "offer_0": {"gil_offer_us": 0},
    "offer_100": {"gil_offer_us": 100},
    "offer_inf": {"gil_offer_us": math.inf},
    "affinity": {"lane_kw": {"select": Affinity}},
    "affinity_early": {"lane_kw": {"select": Affinity, "forming": "early"}},
    "round_robin_greedy": {
        "lane_kw": {"select": RoundRobin},
        "per_kind": {"batch": {"lane": {"former": Greedy}}},
    },
}

# Dispatcher counters both cores keep
STATS = (
    "jobs_ok",
    "jobs_failed",
    "calls",
    "squashed",
    "reused",
    "wasted",
    "store_hits",
    "store_misses",
    "store_weak",
    "store_errors",
    "snapshot_errors",
    "weak_digests",
    "machine_errors",
    "records_blocked",
    "records_dropped",
    "cycles",
    "policy_errors",
    "store_uncopied",
)


class OrderByKey:
    """An issue policy written in Python: the core calls it back."""

    def order(self, ready: list) -> list:
        return sorted(ready, key=lambda r: (r.key, r.job))

    def priority(self, st) -> int:
        return len(st.key) - st.job


def lanes_view(m) -> dict:
    """Each lane's counters, batch sizes and units (name, state, done, errors, in flight)."""
    out = {}
    for lane in m.lanes:
        units = sorted(
            (st.unit.name, st.state, st.items_done, dict(st.errors), st.inflight)
            for st in lane.state.values()
        )
        out[lane.name] = (dict(lane.stats), dict(lane.batch_sizes), units)
    return out


def stats_view(m) -> dict:
    s = m.dispatcher.stats
    return {k: s.get(k) for k in STATS}


def alike(py, rs):
    """The Rust trace ``rs`` equal to the Python trace ``py``."""
    assert type(rs.machine.dispatcher).__name__ == "NativeDispatcher"
    miss = py.log.first_mismatch(rs.log)
    assert miss is None, str(miss)
    diff = first_difference(py.records, rs.records)
    assert diff is None, diff
    assert (py.cycles, py.clock_s) == (rs.cycles, rs.clock_s)
    assert stats_view(py.machine) == stats_view(rs.machine)
    assert lanes_view(py.machine) == lanes_view(rs.machine)


def with_regions(factory):
    """``factory("rust")`` with ``gil_offer_us`` 0 when the machine leaves it at None: the
    scheduling regions run at every chance, each released."""

    def make():
        m = factory("rust")
        if m.knobs["gil_offer_us"] is None:
            m.knobs["gil_offer_us"] = 0
        return m

    return make


def both(factory, jobs, seed: int = 7, log: bool = True, regions: bool = True):
    """The trace of each core, checked equal; returns both. The Rust core runs twice when the
    machine keeps the default ``gil_offer_us`` (None, no regions): as built, and with regions
    at every chance, so every shape checks both paths (``regions=False``: once, for machines
    sharing state across runs, which run both paths themselves)."""
    py = run_trace(lambda: factory("python"), jobs, seed=seed, log=log)
    rs = run_trace(lambda: factory("rust"), jobs, seed=seed, log=log)
    assert type(py.machine.dispatcher).__name__ == "Dispatcher"
    alike(py, rs)
    if regions and rs.machine.knobs["gil_offer_us"] is None:
        alike(py, run_trace(with_regions(factory), jobs, seed=seed, log=log))
    return py, rs


def gen_make(p, **kw):
    """``make(core)`` of a generated machine with item failures and an outage of unit 0."""
    kw = {"item_error_rate": 0.05, "outages": [(0.004, 0.02)], **kw}
    return lambda core: generated(p.text, p.entry, core=core, **kw)()


# ------------------------------------------------------------ the generated suite
@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("gen_seed", GEN_SEEDS)
def test_generated_programs_trace_alike(gen_seed, config):
    p = entry_program(gen_seed, jobs=16)
    py, _ = both(gen_make(p, **CONFIGS[config]), p.inputs)
    assert len(py.log) > 0 and len(py.records) == len(p.inputs)


@pytest.mark.parametrize("config", ["default", "lane_aware", "affinity_early"])
@pytest.mark.parametrize("seed", [2, 13])
@pytest.mark.parametrize("gen_seed", range(6))
def test_other_trace_seeds_trace_alike(gen_seed, seed, config):
    p = entry_program(gen_seed, jobs=16)
    both(gen_make(p, **CONFIGS[config]), p.inputs, seed=seed)


@pytest.mark.parametrize("gen_seed", range(4))
def test_a_python_policy_traces_alike(gen_seed):
    p = entry_program(gen_seed, jobs=16)
    both(gen_make(p, issue=OrderByKey()), p.inputs)


@pytest.mark.parametrize("gen_seed", range(4))
def test_the_path_with_no_listener_runs_alike(gen_seed):
    """No decision log on the bus: regions keep no events; records, time and counters agree."""
    p = entry_program(gen_seed, jobs=16)
    _, rs = both(gen_make(p, record=None, gil_offer_us=0), p.inputs, log=False)
    r = rs.machine.dispatcher.native_stats()
    assert r["regions"] > 0 and r["free_steps"] + r["free_orders"] + r["free_retires"] > 0, r


def test_the_suite_covers_faults_squashes_and_gil_free_work():
    """The parity above is not vacuous: the generated runs fault and squash, and the Rust core
    fetches, orders and retires without the GIL, leaving some steps for it."""
    kinds = {}
    regions = {}
    for g in range(4):
        p = entry_program(g, jobs=16)
        for knobs in ({}, {"record": None}):
            _, rs = both(gen_make(p, gil_offer_us=0, **knobs), p.inputs)
            for k, n in rs.log.counts().items():
                kinds[k] = kinds.get(k, 0) + n
            for k, n in rs.machine.dispatcher.native_stats().items():
                regions[k] = regions.get(k, 0) + n
    assert {"fault", "squash", "submit", "retire", "end"} <= set(kinds), kinds
    assert regions["regions"] > 0 and regions["gil_steps"] > 0, regions
    assert regions["free_steps"] > regions["gil_steps"], regions
    assert regions["free_orders"] > 0 and regions["free_retires"] > 0, regions


# ------------------------------------------------------------ lane subclasses
class PrioLane(Lane):
    """Overrides ``enqueue``: stays a Python lane on the Rust core."""

    def enqueue(self, item, now):
        self._n = getattr(self, "_n", 0) + 1
        if self._n % 2:
            item.priority = 5
        super().enqueue(item, now)


class SlowPumpLane(Lane):
    """Overrides ``pump``: stays a Python lane on the Rust core."""

    def pump(self, now):
        self._n = getattr(self, "_n", 0) + 1
        return 0 if self._n % 2 else super().pump(now)


class TaggedLane(Lane):
    """Overrides nothing the native lane runs: adopted, its class kept as a base."""

    def tag(self) -> str:
        return f"lane {self.name}"


@pytest.mark.parametrize("cls", [PrioLane, SlowPumpLane, TaggedLane])
@pytest.mark.parametrize("gen_seed", range(3))
def test_lane_subclasses_trace_alike(gen_seed, cls):
    p = entry_program(gen_seed, jobs=16)
    _, rs = both(gen_make(p, lane_cls=cls), p.inputs)
    for lane in rs.machine.lanes:
        assert isinstance(lane, cls)
        assert isinstance(lane, NativeLane) == (cls is TaggedLane)
    if cls is TaggedLane:
        assert all(x.tag() == f"lane {x.name}" for x in rs.machine.lanes)


# ------------------------------------------------------------ policies changed on the instance
def with_instance(attr, fn):
    def make(obj):
        setattr(obj, attr, fn)
        return obj

    return make


INSTANCE = {
    "take_on_size_or_wait": {
        "per_kind": {
            "batch": {
                "lane": {
                    "former": lambda: with_instance("take", lambda b, u, n: 1)(SizeOrWait(8, 1))
                }
            }
        }
    },
    "order_on_least_loaded": {
        "lane_kw": {
            "select": lambda: with_instance("order", lambda us, ln: list(reversed(us)))(
                LeastLoaded()
            )
        }
    },
    "order_on_deepest_first": {
        "issue": with_instance("order", lambda ready: list(ready))(DeepestFirst())
    },
    "allows_on_fixed": {
        "admission": with_instance("allows", lambda pool, sig: pool.jobs < 2)(Fixed(8))
    },
}


@pytest.mark.parametrize("case", INSTANCE)
@pytest.mark.parametrize("gen_seed", range(3))
def test_policies_changed_on_the_instance_trace_alike(gen_seed, case):
    """A builtin policy whose instance sets its own method is called back, as Python calls it."""
    p = entry_program(gen_seed, jobs=16)
    both(gen_make(p, **INSTANCE[case]), p.inputs)


def patch_class_mid_run(m):
    """The batch former's class gets its own ``take`` while the machine runs."""
    type(lane(m, "batch").former).take = lambda self, bucket, unit, now: 1


def fresh_size_or_wait():
    """A ``SizeOrWait`` of a class of its own, patched by ``patch_class_mid_run`` alone."""
    return type("OwnSizeOrWait", (SizeOrWait,), {})(8, 1)


@pytest.mark.parametrize("n", [3, 40])
@pytest.mark.parametrize("gen_seed", range(3))
def test_a_policy_class_patched_mid_run_traces_alike(gen_seed, n):
    """A builtin policy's class patched after the native lane first ran it runs the patch."""
    p = entry_program(gen_seed, jobs=16)
    base = gen_make(p, per_kind={"batch": {"lane": {"former": fresh_size_or_wait}}})
    both(lambda core: at_retire(base(core), n, patch_class_mid_run), p.inputs)


# ------------------------------------------------------------ lane methods set on the instance
# Every public lane method the core or the lane calls on itself (``native.DIRECT``)
LANE_METHODS = (
    "enqueue",
    "pump",
    "timers",
    "next_deadline",
    "results",
    "cancel",
    "queued",
    "units_up",
    "queued_now",
    "under_cap",
    "load_waiting",
    "remove",
    "unit_down",
    "requeue_front",
    "rate",
)
# Methods a run without cancels, retries, Throughput, LaneAware or lane signals may not call
RARE_LANE_CALLS = {"cancel", "remove", "requeue_front", "rate", "unit_down", "queued"}
RARE_LANE_CALLS |= {"load_waiting"}
LANE_CONFIGS = {
    "default": {},
    "early_affinity": {"lane_kw": {"select": Affinity, "forming": "early"}},
    "throughput_lane_aware_target_load": {
        "lane_kw": {"select": Throughput},
        "issue": LaneAware(),
        "admission": TargetLoad("call", 64),
    },
}


def counting(m, name: str, calls: dict):
    """Each lane's ``name`` replaced on the instance by a wrapper counting its calls."""
    for ln in m.lanes:
        inner = getattr(ln, name)

        def wrap(*args, _inner=inner, _key=ln.name, **kwargs):
            calls[_key] = calls.get(_key, 0) + 1
            return _inner(*args, **kwargs)

        setattr(ln, name, wrap)
    return m


@pytest.mark.parametrize("config", LANE_CONFIGS)
@pytest.mark.parametrize("name", LANE_METHODS)
@pytest.mark.parametrize("gen_seed", range(2))
def test_lane_methods_set_on_the_instance_are_called_as_python_calls_them(gen_seed, name, config):
    """A lane method set on the instance is called at every point the Python lane calls it,
    from the core and from inside the lane: the same calls per lane on both cores."""
    p = entry_program(gen_seed, jobs=16)
    base = gen_make(p, **LANE_CONFIGS[config])
    runs = {"python": [], "rust": []}

    def make(core):
        runs[core].append({})
        return counting(base(core), name, runs[core][-1])

    both(make, p.inputs)
    # One count per run: the Rust core runs without regions and with them
    (calls,) = runs["python"]
    assert runs["rust"] == [calls, calls]

    # Not vacuous: Throughput, LaneAware and TargetLoad call what the other configs may not
    rare = RARE_LANE_CALLS - (
        {"queued", "load_waiting", "rate"} if "throughput" in config else set()
    )
    assert calls or name in rare, name


def units_up_reversed(m):
    ln = lane(m, "call")
    inner = ln.units_up
    ln.units_up = lambda: list(reversed(inner()))
    return m


def under_cap_always(m):
    ln = lane(m, "call")
    ln.max_inflight = 2
    ln.under_cap = lambda: True
    return m


def unit_down_ignored(m):
    for ln in m.lanes:
        ln.unit_down = lambda unit, err, now: None
    return m


LANE_OVERRIDES = {
    "units_up_reversed": units_up_reversed,
    "under_cap_always": under_cap_always,
    "unit_down_ignored": unit_down_ignored,
}


@pytest.mark.parametrize("case", LANE_OVERRIDES)
@pytest.mark.parametrize("gen_seed", range(3))
def test_lane_methods_that_change_behaviour_trace_alike(gen_seed, case):
    """An override that changes what the lane does changes it the same way on both cores."""
    p = entry_program(gen_seed, jobs=16)
    base = gen_make(p, outages=[(0.002, 0.01)])
    both(lambda core: LANE_OVERRIDES[case](base(core)), p.inputs)


@pytest.mark.parametrize("gen_seed", range(3))
def test_a_lane_that_never_has_work_stalls_alike(gen_seed):
    """``queued_now`` answering 0 keeps the pump from submitting: both cores stall the same."""
    p = entry_program(gen_seed, jobs=16)
    base = gen_make(p)

    def make(core):
        m = base(core)
        lane(m, "call").queued_now = lambda: 0
        return m

    errors = {}
    for core in ("python", "rust"):
        try:
            run_trace(lambda core=core: make(core), p.inputs, seed=7)
        except Exception as e:  # noqa: BLE001
            errors[core] = (type(e).__name__, str(e))
    if errors:
        assert errors.get("python") == errors.get("rust")


# ------------------------------------------------------------ shapes of lanes, units and jobs
class NosyEngine(ScriptedEngine):
    """Reads its lane from ``capacity`` and ``submit`` (the lane is busy calling it)."""

    lane = None

    def peek(self):
        if self.lane is not None:
            ln = self.lane
            ln.queued(), ln.queued_now(), ln.units_up(), ln.under_cap(), ln.signals()
            _ = ln.stats, ln.batch_sizes, ln.inflight, ln.queue, ln.state, ln.subs

    def capacity(self):
        self.peek()
        return super().capacity()

    def submit(self, work, done):
        self.peek()
        return super().submit(work, done)


class NosyFormer(SizeOrWait):
    """Reads its lane from ``key`` and ``take``."""

    lane = None

    def key(self, item, unit):
        if self.lane is not None:
            self.lane.queued(), self.lane.signals()
            _ = self.lane.queue
        return None

    def take(self, bucket, unit, now):
        if self.lane is not None:
            _ = self.lane.queue, self.lane.state, self.lane.stats
            self.lane.signals()
        return super().take(bucket, unit, now)


class NosySelect(LeastLoaded):
    """Reads the lane from ``order``."""

    def order(self, units, lane):
        lane.signals()
        _ = lane.queue, lane.state, lane.subs
        return super().order(units, lane)


def lanes_known(m):
    """Nosy units and formers learn their lane."""
    for ln in m.lanes:
        for u in ln.units:
            u.lane = ln
        if isinstance(ln.former, NosyFormer):
            ln.former.lane = ln
    return m


class SlotLane(Lane):
    """A lane subclass with ``__slots__``: adopted as any other."""

    __slots__ = ("extra",)


SHAPES = {
    "nosy_engine": {"engine_cls": NosyEngine},
    "nosy_engine_early": {"engine_cls": NosyEngine, "lane_kw": {"forming": "early"}},
    "nosy_former": {"per_kind": {"batch": {"lane": {"former": NosyFormer}}}},
    "nosy_select": {"lane_kw": {"select": NosySelect}},
    "float_max_batch_size_or_wait": {
        "per_kind": {
            "batch": {
                "engine": {"max_batch": 2.5},
                "lane": {"former": lambda: SizeOrWait(3, 1)},
            }
        }
    },
    "float_max_batch_greedy": {
        "per_kind": {"batch": {"engine": {"max_batch": 2.5}, "lane": {"former": Greedy}}}
    },
    "float_max_batch_size_or_wait_float_size": {
        "per_kind": {
            "batch": {
                "engine": {"max_batch": 3.0},
                "lane": {"former": lambda: SizeOrWait(2.5, 1)},
            }
        }
    },
    "slot_lane": {"lane_cls": SlotLane},
}


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("gen_seed", range(4))
def test_lane_and_unit_shapes_trace_alike(gen_seed, shape):
    p = entry_program(gen_seed, jobs=16)
    base = gen_make(p, **SHAPES[shape])
    both(lambda core: lanes_known(base(core)), p.inputs)


def test_a_float_batch_cap_is_the_python_number():
    """The batch cap a former reads is Python's ``min(max(1, max_batch or 1))``, an int or a
    float as Python gives it."""
    p = entry_program(0, jobs=4)
    for max_batch, cap in ((2.5, 2.5), (16, 16), (0, 1), (0.5, 1)):
        caps = {}
        for core in ("python", "rust"):
            kw = {"per_kind": {"batch": {"engine": {"max_batch": max_batch}}}}
            m = gen_make(p, **kw)(core)
            seen = []
            former = lane(m, "batch").former
            former.deadline = lambda bucket, now, seen=seen, inner=former.deadline: (
                seen.append(bucket.cap) or inner(bucket, now)
            )
            run_trace(lambda m=m: m, p.inputs, seed=7)
            caps[core] = seen
        assert caps["python"] == caps["rust"] and caps["python"], max_batch
        assert {(type(c), c) for c in caps["python"]} == {(type(cap), cap)}


# A call of a generator node on a lane, with its indentation
LANE_CALL = re.compile(rf"^(\s*)\(.*\)({'|'.join(LANE_OF)})\(")


def huge_priorities(text: str) -> str:
    """Every lane call of a generated program hinted with a priority past 2**128."""
    out = []
    for i, line in enumerate(text.splitlines()):
        m = LANE_CALL.match(line)
        if m:
            out.append(f"{m.group(1)}@hint priority={2**130 + (i % 3)}")
        out.append(line)
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("gen_seed", range(6))
def test_huge_priorities_order_exactly_alike(gen_seed):
    """Priorities 2**130, 2**130 + 1 and 2**130 + 2 order the lane queues as Python orders them."""
    p = entry_program(gen_seed, jobs=16)
    one = {k: {"engine": {"capacity": 1}} for k in CAPACITY}

    def make(core):
        kw = {"issue": HintPriority(), "per_kind": one, "item_error_rate": 0.05}
        return generated(huge_priorities(p.text), p.entry, core=core, **kw)()

    both(make, p.inputs)


class Named:
    """A key object whose ``str()`` is a parameter's name."""

    def __init__(self, s):
        self.s = s

    def __str__(self):
        return self.s

    def __repr__(self):
        return f"Named({self.s})"


def job_inputs(base: list) -> dict:
    first = next(iter(base[0]))
    rest = [{k: v for k, v in x.items() if k != first} for x in base]
    return {
        "int_input_key": [SourceItem(str(i), {**x, 5: 1}) for i, x in enumerate(base)],
        "named_key": [
            SourceItem(str(i), {**r, Named(first): x[first]})
            for i, (x, r) in enumerate(zip(base, rest, strict=True))
        ],
        "mixed_unknown_keys": [
            SourceItem(str(i), {**x, 5: 1, "zz": 2}) for i, x in enumerate(base)
        ],
        "ordered_dict": [SourceItem(str(i), OrderedDict(x)) for i, x in enumerate(base)],
        "mapping_proxy": [
            SourceItem(str(i), types.MappingProxyType(dict(x))) for i, x in enumerate(base)
        ],
        "int_job_keys": [SourceItem(i % 3, dict(x)) for i, x in enumerate(base)],
        "bool_and_int_job_keys": [
            SourceItem(True if i == 1 else i, dict(x)) for i, x in enumerate(base)
        ],
        "tuple_job_keys": [SourceItem((i, "k"), dict(x)) for i, x in enumerate(base)],
    }


@pytest.mark.parametrize("case", job_inputs([{"a": 0}]))
def test_job_inputs_and_keys_of_any_shape_run_alike(case):
    """Inputs as any mapping, unknown input keys of any type, and job keys of any type: the same
    records (rejections and their messages included) on both cores."""
    p = entry_program(0, jobs=8)
    items = job_inputs(list(p.inputs))[case]
    py, _ = both(gen_make(p), items)
    assert len(py.records) == len(items)
    if case in ("int_input_key", "named_key"):
        assert all(not r.ok and "unknown inputs" in str(r.failure) for r in py.records)
    elif case == "mixed_unknown_keys":
        # Sorting an int with a str raises, in both cores as in Python
        assert all(not r.ok and "not supported" in str(r.failure) for r in py.records)
    else:
        assert any(r.ok for r in py.records)


# ------------------------------------------------------------ bus listeners and the GIL
def listener():
    def f(kind, fields):
        pass

    return f


@pytest.mark.parametrize(
    "steps, acting",
    [
        ((("listen", None, True), ("listen", None, False)), 1),
        ((("listen", None, False), ("listen", None, True)), 1),
        ((("listen", ("a",), True), ("listen", ("b",), False), ("unlisten",)), 0),
        ((("listen", ("a",), False), ("listen", ("b",), True), ("unlisten",)), 0),
        ((("listen", ("a",), True), ("listen", ("b",), True)), 0),
        ((("listen", None, True), ("unlisten",), ("listen", ("a",), False)), 1),
    ],
)
def test_a_bus_listener_is_passive_only_while_every_registration_is(steps, acting):
    """Passivity is per function: one acting registration makes it acting; unlisten recounts."""
    p = entry_program(0, jobs=4)
    m = gen_make(p, gil_offer_us=0)("rust")
    fn = listener()
    before = m.bus.acting
    for step in steps:
        match step:
            case ("listen", kinds, passive):
                m.bus.listen(fn, kinds, passive=passive)
            case ("unlisten",):
                m.bus.unlisten(fn)
    assert m.bus.acting - before == acting

    # An acting listener keeps every cycle on the GIL path; a passive one lets regions run
    t = run_trace(lambda: m, p.inputs, seed=7)
    regions = t.machine.dispatcher.native_stats()["regions"]
    assert (regions == 0) == (m.bus.acting > 0), (regions, m.bus.acting)


@pytest.mark.parametrize("acting", [False, True])
@pytest.mark.parametrize("offer_us", [0, 100, math.inf, None])
def test_gil_offers_follow_the_knob_with_or_without_acting_listeners(offer_us, acting):
    """``gil_offer_us`` sets the GIL offers: at most one per interval on the core's clock, none
    for inf and None, and pure offers at a cycle's end when no region made one (an acting
    listener runs no region). Inf runs every region held; 0 releases every one."""
    p = entry_program(0, jobs=16)
    m = gen_make(p, gil_offer_us=offer_us)("rust")
    if acting:
        m.bus.listen(listener())
    t0 = time.perf_counter()
    t = run_trace(lambda: m, p.inputs, seed=7)
    wall = time.perf_counter() - t0
    s = t.machine.dispatcher.native_stats()
    assert s["regions"] == s["regions_released"] + s["regions_held"]
    assert s["offers"] == s["regions_released"] + s["pure_offers"]
    match offer_us, acting:
        case None, _:
            assert s["offers"] == 0 and s["regions"] == 0, s
        case math.inf, False:
            assert s["offers"] == 0 and s["regions"] == s["regions_held"] > 0, s
        case math.inf, True:
            assert s["offers"] == 0 and s["regions"] == 0, s
        case _, True:
            assert s["regions"] == 0 and s["offers"] == s["pure_offers"] > 0, s
        case 0, False:
            assert s["regions_held"] == 0 and s["regions_released"] > 0, s
        case _:
            assert s["regions"] > 0, s
    if offer_us:
        assert s["offers"] <= wall / (offer_us * 1e-6) + 1, (s, wall)


class Neighbour(threading.Thread):
    """A Python thread that counts its turns, giving the GIL back after each: with a long
    switch interval it runs only when the dispatcher thread really lets go of the GIL."""

    def __init__(self):
        super().__init__(daemon=True)
        self.turns = 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            self.turns += 1
            time.sleep(0)


def neighbour_turns(offer_us) -> tuple[int, dict]:
    """The neighbour's turns during one traced run at ``offer_us``, and the core's counts."""
    p = entry_program(0, jobs=16)
    m = gen_make(p, gil_offer_us=offer_us)("rust")
    old = sys.getswitchinterval()
    sys.setswitchinterval(1.0)
    n = Neighbour()
    try:
        n.start()
        while n.turns == 0:
            time.sleep(0.001)
        start = n.turns
        t = run_trace(lambda: m, p.inputs, seed=7)
        turns = n.turns - start
    finally:
        n.stop.set()
        sys.setswitchinterval(old)
        n.join()
    return turns, t.machine.dispatcher.native_stats()


def test_an_offer_really_lets_a_waiting_thread_run():
    """Offers are real GIL releases: a neighbour thread gets far more turns at 0 (every region
    released) than at inf (every region held, no offer), whatever the counters say. How many
    releases hand over the GIL depends on the platform's scheduler: on Linux the releasing
    thread usually takes it back before the neighbour wakes, so a run of about 570 offers gives
    it 3 to 18 turns and a held run 0 to 3; and now and then a forced switch hands the neighbour
    thousands of turns in either mode. The check compares the medians of ten runs each."""
    runs = {offer_us: [neighbour_turns(offer_us) for _ in range(10)] for offer_us in (0, math.inf)}
    every = statistics.median(turns for turns, _ in runs[0])
    never = statistics.median(turns for turns, _ in runs[math.inf])
    assert all(s["offers"] > 0 for _, s in runs[0]), runs[0]
    assert all(s["offers"] == 0 for _, s in runs[math.inf]), runs[math.inf]
    turns = {k: [t for t, _ in v] for k, v in runs.items()}
    assert every >= 2 and every > 2 * never, (every, never, turns)


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("name", ["gil_offer_us", "inline_warn_us"])
def test_a_number_knob_past_the_float_range_is_refused(name, core):
    """An int too large for a float is refused by the knob check, at construction and when set,
    instead of stopping the dispatcher later."""
    p = entry_program(0, jobs=4)
    with pytest.raises(ConstructionError, match=name):
        gen_make(p, **{name: 10**400})(core)
    m = gen_make(p, **{name: math.inf})(core)
    value, problem = checked(name, 10**400, {})
    assert problem is not None and name in problem
    assert checked(name, math.inf, {})[1] is None
    t = run_trace(lambda: m, p.inputs, seed=7)
    assert len(t.records) == len(p.inputs)


# ------------------------------------------------------------ changes made mid-run
def lane(m, name):
    return next(x for x in m.lanes if x.name == name)


class _Reply:
    def _set(self, value=None, error=None):
        pass


def command(name, /, **args):
    """A command answered as the dispatcher answers it."""
    return lambda m: m.dispatcher.answer(m.commands, "command", name, args, _Reply())


def new_unit(m, kind, name, latency=0.7):
    old = lane(m, kind).units[0]
    u = ScriptedEngine(kind, name=name, latency_ms=latency, capacity=CAPACITY[kind])
    u.bind(old.schedule, 99)
    return u


def unit_down(m):
    ln = lane(m, "call")
    ln.unit_down(ln.units[0], UnitError(RuntimeError("operator")), m.dispatcher.clock())


def retry_max_zero(m):
    lane(m, "stream").retry.max = 0


MIDRUN = {
    "select_round_robin": lambda m: setattr(lane(m, "call"), "select", RoundRobin()),
    "select_affinity": lambda m: setattr(lane(m, "stream"), "select", Affinity()),
    "select_throughput": command(
        "set_lane_policy", lane="call", policy="select", value=Throughput()
    ),
    "former_greedy": lambda m: setattr(lane(m, "batch"), "former", Greedy()),
    "former_size_or_wait": command(
        "set_lane_policy", lane="batch", policy="former", value=SizeOrWait(3, 0.2)
    ),
    "forming_early": lambda m: setattr(lane(m, "call"), "forming", "early"),
    "knob_window": command("set_knob", name="window", value=2),
    "knob_issue": command("set_knob", name="issue", value=Fifo()),
    "max_inflight": lambda m: setattr(lane(m, "call"), "max_inflight", 1),
    "timeout": lambda m: setattr(lane(m, "call"), "timeout_s", 0.0005),
    "retry": lambda m: setattr(lane(m, "stream"), "retry", Retry(max=0)),
    "retry_max_zero": retry_max_zero,
    "lane_dead": lambda m: setattr(lane(m, "stream"), "lane_dead_s", 0.0),
    "unit_down": unit_down,
    "detach_drain": lambda m: lane(m, "call").detach(
        lane(m, "call").units[0], True, m.dispatcher.clock()
    ),
    "detach_now": lambda m: lane(m, "call").detach(
        lane(m, "call").units[0], False, m.dispatcher.clock()
    ),
    "attach_new": lambda m: lane(m, "call").attach(new_unit(m, "call", "callX")),
    "pause_resume": lambda m: [
        (m.dispatcher.pause_job(j), m.dispatcher.resume_job(j))
        for j in list(m.dispatcher.jobs.values())[:3]
    ],
    "cancel": lambda m: [
        m.dispatcher.cancel_job(j, "probe") for j in list(m.dispatcher.jobs.values())[:2]
    ],
    "stop": lambda m: m.dispatcher.begin_stop(False),
}


def at_retire(m, n, action):
    """Post ``action(m)`` as an inbox call at the n-th stmt.retire event (virtual time fixes it)."""
    box = {"k": 0}

    def on(kind, f):
        box["k"] += 1
        if box["k"] == n:
            m.dispatcher.inbox.put(("call", lambda: action(m)))

    m.bus.listen(on, {"stmt.retire": 1}, passive=True)
    return m


@pytest.mark.parametrize("action", MIDRUN)
@pytest.mark.parametrize("n", [3, 40])
@pytest.mark.parametrize("gen_seed", range(3))
def test_changes_made_mid_run_trace_alike(gen_seed, n, action):
    p = entry_program(gen_seed, jobs=16)
    base = gen_make(p, item_error_rate=0.03)
    both(lambda core: at_retire(base(core), n, MIDRUN[action]), p.inputs)


def small_batch_unit(m, name, max_batch):
    old = lane(m, "batch").units[0]
    u = ScriptedEngine("batch", name=name, latency_ms=0.7, capacity=2, max_batch=max_batch)
    u.bind(old.schedule, 99)
    return u


def detach_then_small(m):
    ln = lane(m, "batch")
    ln.detach(ln.units[0], False, m.dispatcher.clock())
    ln.attach(small_batch_unit(m, "batchS", 2))


# Changes under early forming: items sit in each unit's own queue
EARLY = {
    "detach_drain": MIDRUN["detach_drain"],
    "detach_now": MIDRUN["detach_now"],
    "detach_now_batch": lambda m: lane(m, "batch").detach(
        lane(m, "batch").units[0], False, m.dispatcher.clock()
    ),
    "detach_drain_batch": lambda m: lane(m, "batch").detach(
        lane(m, "batch").units[1], True, m.dispatcher.clock()
    ),
    "detach_then_small": detach_then_small,
    "attach_small_batch": lambda m: lane(m, "batch").attach(small_batch_unit(m, "batchS", 3)),
    "former_greedy": MIDRUN["former_greedy"],
    "forming_shared": lambda m: setattr(lane(m, "batch"), "forming", "shared"),
}


@pytest.mark.parametrize("select", ["least_loaded", "affinity"])
@pytest.mark.parametrize("action", EARLY)
@pytest.mark.parametrize("n", [3, 40])
@pytest.mark.parametrize("gen_seed", range(3))
def test_changes_made_mid_run_under_early_forming_trace_alike(gen_seed, n, action, select):
    """Every run finishes, alike: a detached unit's own queue is emptied into the shared one."""
    p = entry_program(gen_seed, jobs=16)
    lane_kw = {"forming": "early"} | ({"select": Affinity} if select == "affinity" else {})
    sow = {"batch": {"lane": {"former": lambda: SizeOrWait(6, 1)}}}
    base = gen_make(p, outages=[(0.003, 0.012)], lane_kw=lane_kw, per_kind=sow)
    py, _ = both(lambda core: at_retire(base(core), n, EARLY[action]), p.inputs)
    assert len(py.records) == len(p.inputs)


# ------------------------------------------------------------ snapshots and stores
STORED = {
    "content": {"use_kw": {"store": "content"}},
    "positional": {"use_kw": {"store": "positional"}},
    "snapshots": {"snapshot_every": 2},
    "snapshots_content": {"snapshot_every": 3, "use_kw": {"store": "content"}},
}


@pytest.mark.parametrize("offer_us", [None, 0])
@pytest.mark.parametrize("case", STORED)
@pytest.mark.parametrize("gen_seed", range(3))
def test_snapshots_and_stores_over_two_runs_trace_alike(gen_seed, case, offer_us):
    """A first run with failures fills a store; a second run on it hits results and resumes
    snapshots. Both runs, and the stores, agree."""
    p = entry_program(gen_seed, jobs=16)
    stores = {"python": MemoryStore(), "rust": MemoryStore()}
    for rate in (0.15, 0.0):

        def make(core, rate=rate):
            kw = {"item_error_rate": rate, "store": stores[core], **STORED[case]}
            return generated(p.text, p.entry, core=core, gil_offer_us=offer_us, **kw)()

        both(make, p.inputs, regions=False)
    assert sorted(stores["python"].data) == sorted(stores["rust"].data)


# ------------------------------------------------------------ memory
@pytest.mark.parametrize("core", ["python", "rust"])
def test_a_finished_machine_is_freed(core):
    """After a run, the machine, its dispatcher and its lanes go with the last reference; the
    Rust core's jobs, strands, inflights and slots all go too."""
    before = dict(_rs.live_objects())
    p = entry_program(3, jobs=16)
    make = gen_make(p, item_error_rate=0.2)
    t = run_trace(lambda: make(core), p.inputs, seed=7)
    refs = [weakref.ref(t.machine), weakref.ref(t.machine.dispatcher)]
    refs += [weakref.ref(x) for x in t.machine.lanes]
    del t
    gc.collect()
    assert [r() is None for r in refs] == [True] * len(refs)
    assert dict(_rs.live_objects()) == before


# ------------------------------------------------------------ the native digest
class Point:
    def __init__(self, x):
        self.x = x


VALUES = [
    None,
    True,
    False,
    0,
    -7,
    1 << 80,
    1.5,
    -0.0,
    float("inf"),
    float("nan"),
    1e16,
    complex(1, -2),
    "",
    "héllo",
    "\ud800x",
    b"\x00\xff",
    bytearray(b"ab"),
    memoryview(b"xyz"),
    [1, [2, [3]]],
    (1, "a", None),
    {"b": 1, "a": [1, 2], 3: "x", (1, 2): {4}},
    {1, 2.5, "s", (1,)},
    frozenset({"a", "b"}),
    Fraction(1, 3),
    Point(3),
    [Point(1), {"k": Point(2)}],
    {"weak": lambda: 0},
]


def test_the_native_digest_is_the_python_digest():
    cyclic = [1]
    cyclic.append(cyclic)
    for v in [*VALUES, VALUES, {"all": VALUES}, cyclic]:
        assert _rs.digest_with_weak(v) == digest_with_weak(v), repr(v)[:80]


# ------------------------------------------------------------ the examples
def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def scripted(kind: str, units: int = 2, **lane) -> Lane:
    engines = [
        ScriptedEngine(kind, name=f"{kind}{i}", latency_ms=2.0, capacity=CAPACITY[kind])
        for i in range(units)
    ]
    former = SizeOrWait(8, 1) if kind == "batch" else None
    return Lane(engines, former=former, name=kind, **lane)


def example_machine(program: Path, entry: str, lib, lanes: dict, inline: list, core: str, **kw):
    made = {}
    bind = {}
    for node, kind in lanes.items():
        if kind not in made:
            made[kind] = scripted(kind, **kw.pop(f"{kind}_lane", {}))
        bind[node] = Use(made[kind])
    bind.update({n: Inline() for n in [*inline, "kir.std.*"]})
    return Machine(
        program=program, entry=entry, libraries=[lib], bind=bind, record=1.0, core=core, **kw
    )


def test_the_hello_example_traces_alike():
    nodes = module("parity_hello_nodes", EXAMPLES / "hello" / "nodes.py")
    people = [
        {"name": "Ayu", "langs": ["en", "ja", "fr"]},
        {"name": "Bao", "langs": ["zh", "es", "de", "en"]},
        {"name": "Chi", "langs": ["ja"]},
        {"name": "Dev", "langs": ["fr", "xx", "en"]},
        {"name": "Eli", "langs": []},
    ]

    def make(core):
        return example_machine(
            EXAMPLES / "hello" / "hello.kir",
            "greet",
            nodes.lib,
            {"translate": "call"},
            ["compose"],
            core,
        )

    both(make, people)


def test_the_multi_unit_example_traces_alike():
    ex = module("parity_multi_unit", EXAMPLES / "multi_unit" / "machine.py")

    def make(core):
        return example_machine(
            EXAMPLES / "multi_unit" / "multi_unit.kir",
            "squares",
            ex.lib,
            {"square": "call"},
            [],
            core,
            call_lane={"select": Throughput(), "retry": Retry(max=5)},
        )

    both(make, ex.JOBS[:12])


def test_the_trace_replay_example_traces_alike():
    ex = module("parity_trace_replay", EXAMPLES / "trace_replay" / "machine.py")

    def make(core):
        lanes = {"fetch": "stream", "parse": "call", "score": "batch"}
        return example_machine(
            EXAMPLES / "trace_replay" / "pipeline.kir",
            "review",
            ex.lib,
            lanes,
            ["split", "best_of"],
            core,
            admission=TargetLoad("stream", waiting=24),
        )

    both(make, ex.URLS[:16])


def test_the_parallel_example_traces_alike(monkeypatch):
    nodes = module("parity_parallel_nodes", EXAMPLES / "parallel" / "nodes.py")
    monkeypatch.setattr(nodes, "_work", lambda *a: None)
    districts = ["north", "harbor", "old town", "west", "station"]
    cities = [
        {"city": "Kyoto", "districts": districts},
        {"city": "Lisbon", "districts": districts[:3]},
        {"city": "Quito", "districts": districts[1:]},
        {"city": "Oslo", "districts": districts[:2]},
    ]

    def make(core):
        lanes = {n: "call" for n in ("weather", "events", "traffic", "alert", "survey")}
        return example_machine(
            EXAMPLES / "parallel" / "report.kir",
            "report",
            nodes.lib,
            lanes,
            ["is_stormy", "compose"],
            core,
        )

    both(make, cities)


def test_the_mixed_example_traces_alike(monkeypatch):
    pytest.importorskip("torch")
    monkeypatch.syspath_prepend(str(EXAMPLES / "mixed"))
    nodes = module("parity_mixed_nodes", EXAMPLES / "mixed" / "nodes.py")

    def station(i: int) -> dict:
        rng = random.Random(i)
        windows = [
            {"i": w, "samples": [math.sin(t * (1 + w)) + rng.gauss(0, 0.3) for t in range(64)]}
            for w in range(2 + i % 4)
        ]
        return {"station": f"st{i:03d}", "windows": windows}

    def make(core):
        lanes = {
            "lookup": "call",
            "features": "call",
            "embed": "batch",
            "note": "stream",
            "close": "stream",
        }
        return example_machine(
            EXAMPLES / "mixed" / "stations.kir",
            "survey",
            nodes.lib,
            lanes,
            ["classify", "majority"],
            core,
        )

    # Station st007 is unknown to the registry: its lookup fails and the `@try` handler runs
    both(make, [station(i) for i in range(3, 9)])
