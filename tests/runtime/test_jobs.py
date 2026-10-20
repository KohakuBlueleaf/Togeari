"""Job lifecycle: keys, handles, stop, pause, loops, hooks around jobs, signals, knobs."""

import logging
import threading
import time

import pytest
from rt_helpers import LIB, TWO, in_thread, machine, plugin, sim_lane, wait_for

from togeari import Inline, Lane, Machine, PluginBlockError, SourceItem, Use, WorkItem
from togeari.io import Iterable, Memory
from togeari.policies import MemoryBound
from togeari.testing.sim import SimEngine

logging.disable(logging.CRITICAL)

COUNT = """
@def (n: int)main(i):
    i = 0
    ()jump(`top`)
    top:
        (i, 1)add(i)
        (i, n)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`top`)
        done:
"""


def test_a_duplicate_key_is_refused_and_the_first_job_still_answers():
    m = machine(bind={"inc": Use(sim_lane(latency_ms=50))})
    try:
        a = m.submit({"x": 1}, key="k")
        b = m.submit({"x": 2}, key="k")
        assert b.result(5).failure["kind"] == "DuplicateKey"
        assert a.result(5).outputs == {"y": 2}
    finally:
        m.stop()


def test_a_key_rewritten_by_pre_admit_still_resolves_its_handle():
    p = plugin("rekey", pre_admit=lambda item, **ctx: SourceItem("new-" + item.key, item.inputs))
    m = machine(plugins=[p])
    try:
        rec = m.submit({"x": 1}, key="old").result(5)
        assert rec.key == "new-old" and rec.ok
    finally:
        m.stop()


def test_a_blocked_record_is_counted_and_its_handle_answered():
    def block(record):
        raise PluginBlockError("not this one")

    m = machine(plugins=[plugin("gate", pre_sink=block)])
    try:
        rec = m.submit({"x": 1}).result(5)
        assert rec.stats["sink_blocked"] == "not this one"
        assert m.inspect("stats").result()["records_blocked"] == 1
    finally:
        m.stop()


def test_stop_without_drain_cancels_and_answers_resident_jobs():
    m = machine(bind={"inc": Use(sim_lane(latency_ms=2000))})
    h = m.submit({"x": 1})
    assert wait_for(lambda: m.signals()["jobs"] == 1)
    finished, _ = in_thread(lambda: m.stop(drain=False), 5)
    assert finished
    assert h.result(1).failure["kind"] == "Cancelled"


def test_stop_with_drain_finishes_resident_jobs():
    m = machine(bind={"inc": Use(sim_lane(latency_ms=100))})
    h = m.submit({"x": 1})
    m.stop(drain=True)
    assert h.result(1).outputs == {"y": 2}


def test_a_paused_job_with_ready_work_does_not_spin():
    m = machine(program=TWO, bind={"inc": Use(sim_lane(latency_ms=150, capacity=1))})
    try:
        h = m.submit({"x": 1})
        jid = m.inspect("jobs").result()[0]["id"]
        m.command("pause_job", id=jid).result()
        time.sleep(0.3)
        c0 = m.dispatcher.stats["cycles"]
        time.sleep(0.3)
        assert m.dispatcher.stats["cycles"] - c0 < 50
        m.command("resume_job", id=jid).result()
        assert h.result(5).outputs == {"y": 3}
    finally:
        m.stop()


def test_a_long_call_free_loop_runs_past_the_fetch_budget():
    prog = "@def (xs: list)main(y):\n    y = 0\n    (xs)each(i, `body`)\n    body:\n        y = i\n"
    m = Machine(program=prog, entry="main", libraries=[LIB], bind={"kir.std.*": Inline()})
    try:
        finished, rec = in_thread(lambda: m.submit({"xs": list(range(15000))}).result(10), 12)
        assert finished and rec.outputs == {"y": 14999}
    finally:
        m.stop()


def test_the_step_budget_fails_an_endless_loop():
    m = Machine(
        program=COUNT, entry="main", libraries=[LIB], bind={"kir.std.*": Inline()}, step_budget=500
    )
    try:
        rec = m.submit({"n": 10**9}).result(5)
        assert rec.failure["kind"] == "KirRuntimeError" and "500 steps" in rec.failure["message"]
    finally:
        m.stop()


def test_a_pre_submit_replacement_item_keeps_its_owner():
    def swap(item, **ctx):
        new = WorkItem(item.node, dict(item.inputs), item.adapter)
        return new

    m = machine(plugins=[plugin("swap", pre_submit=swap)])
    try:
        finished, rec = in_thread(lambda: m.submit({"x": 1}).result(3), 4)
        assert finished and rec.outputs == {"y": 2}
    finally:
        m.stop()


def test_a_timeout_hint_sets_the_item_deadline():
    seen = []
    prog = "@def (x: int)main(y):\n    @hint timeout_s=2.5\n    (x)inc(y)\n"
    m = machine(program=prog, plugins=[plugin("see", pre_submit=lambda it, **c: seen.append(it))])
    try:
        assert m.submit({"x": 1}).result(5).ok
        assert seen[0].deadline is not None and seen[0].deadline > time.monotonic()
    finally:
        m.stop()


def test_applies_to_entries_is_enforced():
    ends = []
    p = plugin("other", applies_to={"entries": ["other"]}, on_job_end=ends.append)
    m = machine(plugins=[p])
    try:
        assert m.submit({"x": 1}).result(5).ok
        assert ends == []
    finally:
        m.stop()


def test_set_knob_record_takes_effect_and_set_knob_checks_values():
    m = machine()
    try:
        assert m.submit({"x": 1}).result(5).record is None
        m.command("set_knob", name="record", value=1.0).result()
        assert m.submit({"x": 2}).result(5).record is not None
        with pytest.raises(ValueError):
            m.command("set_knob", name="window", value="wide").result()
        with pytest.raises(ValueError):
            m.command("set_knob", name="mutates", value="sometimes").result()
    finally:
        m.stop()


def test_the_stats_query_is_a_deep_copy():
    m = machine()
    try:
        m.submit({"x": 1}).result(5)
        s = m.inspect("stats").result()
        s["phase_s"]["mine"] = 1.0
        assert "mine" not in m.inspect("stats").result()["phase_s"]
    finally:
        m.stop()


def test_job_query_counts_slots_of_every_strand():
    prog = """
@def (x: int)main(y):
    (x)isbig(c)
    (c)branch(`yes`, `no`)
    yes:
        (x)inc(a)
    no:
        a = 0
    (x)isbig(d)
    (d)branch(`yes2`, `no2`)
    yes2:
        (x)inc(b)
    no2:
        b = 1
    (a, b)add(y)
"""
    lane = sim_lane(latency_ms=300)
    m = machine(program=prog, bind={"inc": Use(lane), "isbig": Use(lane)})
    try:
        h = m.submit({"x": 1})
        jid = m.inspect("jobs").result()[0]["id"]

        # Two parked strands: the first holds `c` pending, the second also the phi `a` and `d`
        assert wait_for(lambda: len(m.inspect("job", id=jid).result()["strands"]) == 2)
        slots = m.inspect("job", id=jid).result()["slots"]
        assert slots["pending"] >= 3
        h.result(10)
    finally:
        m.stop()


def test_an_idle_machine_waiting_on_its_source_does_not_poll():
    gate = threading.Event()

    def source():
        yield {"x": 1}
        gate.wait(1.0)
        yield {"x": 2}

    m = machine()
    try:
        t = threading.Thread(target=lambda: m.run(source(), Memory()), daemon=True)
        t.start()
        time.sleep(0.3)
        c0 = m.dispatcher.stats["cycles"]
        time.sleep(0.5)
        assert m.dispatcher.stats["cycles"] - c0 <= 2
        gate.set()
        t.join(5)
        assert not t.is_alive()
    finally:
        m.stop()


class Counted(SimEngine):
    loads = 0
    threads: set = set()

    def load(self):
        Counted.loads += 1
        Counted.threads.add(threading.get_ident())
        return super().load()


def test_fixed_admission_never_builds_lane_signals():
    lane = sim_lane(latency_ms=0.5)
    calls = []
    real = lane.signals
    lane.signals = lambda: calls.append(1) or real()
    m = machine(bind={"inc": Use(lane)})
    try:
        m.run(Iterable([{"x": i} for i in range(200)]), Memory())
        assert calls == []
    finally:
        m.stop()


def test_plugin_signals_from_another_thread_load_units_on_the_dispatcher():
    Counted.threads = set()
    holder = {}
    lane = Lane([Counted(name="counted2", latency_ms=0.5)], name="counted2")
    m = machine(
        bind={"inc": Use(lane)},
        plugins=[plugin("sig", on_load=lambda c: holder.setdefault("c", c))],
    )
    try:
        m.submit({"x": 1}).result(5)
        sig = holder["c"].signals()
        assert sig["jobs"] == 0 and "counted2" in sig["units"]
        assert Counted.threads == {m.dispatcher.thread.ident}
        assert not hasattr(m, "signals_now")
    finally:
        m.stop()


def test_memory_bound_admission_sees_job_bytes():
    peak = []
    p = plugin("peak", vote_admit=lambda sig: peak.append(sig["jobs"]) or True)
    m = machine(
        program="@def (x: int, pad: str)main(y):\n    (x)inc(y)\n",
        bind={"inc": Use(sim_lane(latency_ms=20, capacity=64))},
        admission=MemoryBound(30_000),
        plugins=[p],
    )
    try:
        m.run(Iterable([{"x": i, "pad": "z" * 10_000} for i in range(20)]), Memory())
        assert max(peak) <= 3
    finally:
        m.stop()
