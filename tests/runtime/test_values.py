"""Values the machine keeps: stores, records, snapshots, undefined names, payloads, digests."""

import hashlib
import logging
import pickle
import threading
import time
from types import SimpleNamespace

from rt_helpers import CALLS, machine, sim_lane

from togeari import Inline, Use, WorkItem
from togeari.conformance import conform
from togeari.core.contracts.digest import digest, register_digest, unregister_digest
from togeari.core.contracts.io import MISS
from togeari.core.runtime.frontend.fetch import FrontEnd
from togeari.core.runtime.kir import UNDEF
from togeari.core.runtime.retire import snapshot
from togeari.core.runtime.state import Job, Join, NameMap, Slot
from togeari.io import Iterable, Memory
from togeari.stores import MemoryStore

LOOP = """
@def (n: int, tag: str)main(x):
    x = 0
    ()jump(`top`)
    top:
        (x)inc(x)
        (x, n)less(more)
        (more)branch(`again`, `done`)
        again:
            ()jump(`top`)
        done:
"""
PUSH = "@def (x: int)main(n, xs):\n    (x)mklist(xs)\n    (xs)push(n)\n"


def test_content_store_hits_never_alias_a_later_in_place_change():
    store = MemoryStore()
    m = machine(
        program=PUSH,
        bind={"mklist": Use(sim_lane(), store="content"), "push": Inline()},
        store=store,
    )
    try:
        for _ in range(2):
            sink = Memory()
            m.run(Iterable([{"x": 7}] * 3), sink)
            assert all(r.outputs == {"n": 2, "xs": [7, 99]} for r in sink.records)
        assert list(store.data.values()) == [[7]]
    finally:
        m.stop()


def test_records_keep_results_from_before_a_later_in_place_change():
    for mode in ("copy-if-speculative", "wait", "copy-always"):
        m = machine(
            program=PUSH,
            bind={"mklist": Use(sim_lane()), "push": Inline()},
            record=1.0,
            mutates=mode,
        )
        try:
            rec = m.submit({"x": 7}).result(5)
            assert rec.record[0][3] == [7], mode
            v = conform(m.code.entries["main"].region.job, rec, {"x": 7})
            assert v.ok, (mode, v.reason)
        finally:
            m.stop()


def test_a_node_returning_none_is_cached():
    CALLS.clear()
    prog = "@def (x: int)main(y):\n    (x)nothing(y)\n"
    m = machine(
        program=prog, bind={"nothing": Use(sim_lane(), store="content")}, store=MemoryStore()
    )
    try:
        for _ in range(3):
            assert m.submit({"x": 5}).result(5).ok
        assert CALLS == [5]
    finally:
        m.stop()


def test_positional_results_are_written_to_an_empty_memory_store():
    store = MemoryStore()
    m = machine(bind={"inc": Use(sim_lane(), store="positional")}, store=store)
    try:
        m.run(Iterable([{"x": i} for i in range(5)]), Memory())
        assert len(store) == 5
    finally:
        m.stop()


def loop_machine(store, latency_ms=0.2, **kw):
    # Flat latency: the cancel tests stop a job at a known point in time
    lane = sim_lane(latency_ms=latency_ms, tail=0.0)
    return machine(program=LOOP, bind={"inc": Use(lane)}, store=store, snapshot_every=3, **kw)


def cancel_after(m, h, seconds):
    time.sleep(seconds)
    jobs = m.inspect("jobs").result()
    assert jobs, "the job ended before it was cancelled"
    m.command("cancel_job", id=jobs[0]["id"]).result()
    return h.result(5)


def test_a_snapshot_resumes_only_the_same_job_and_is_gone_once_it_succeeds():
    store = MemoryStore()

    # A first run of key "k" stops halfway (cancelled), leaving a snapshot
    m = loop_machine(store)
    try:
        cancel_after(m, m.submit({"n": 10**6, "tag": "a"}, key="k"), 0.3)
    finally:
        m.stop()
    assert len(store) == 1

    # Same key, other inputs: starts over
    m = loop_machine(store)
    try:
        other = m.submit({"n": 3, "tag": "b"}, key="k").result(5)
        assert not other.stats.get("resumed") and other.outputs == {"x": 3}
    finally:
        m.stop()

    # Same key and inputs: resumes
    m = loop_machine(store)
    try:
        rec = cancel_after(m, m.submit({"n": 10**6, "tag": "a"}, key="k"), 0.2)
        assert rec.stats.get("resumed")
    finally:
        m.stop()

    # A job that succeeds leaves no snapshot behind
    m = loop_machine(store)
    try:
        assert m.submit({"n": 4, "tag": "c"}, key="z").result(5).ok
    finally:
        m.stop()
    assert len(store) == 1


def test_an_unpicklable_value_is_reported_at_the_first_snapshot():
    prog = LOOP.replace("    x = 0\n", "    (n)lock(lk)\n    x = 0\n")
    m = machine(
        program=prog,
        bind={"inc": Use(sim_lane(latency_ms=0.2)), "lock": Inline()},
        store=MemoryStore(),
        snapshot_every=1,
        run_ahead=False,
    )
    try:
        rec = m.submit({"n": 5, "tag": "a"}).result(5)
        assert rec.ok and rec.outputs == {"x": 5}
        assert "snapshot_error" in rec.stats
        assert m.dispatcher.thread.is_alive()
    finally:
        m.stop()


def test_a_resumed_recorded_job_conforms():
    store = MemoryStore()
    m = loop_machine(store, latency_ms=20, record=1.0)
    try:
        cancel_after(m, m.submit({"n": 40, "tag": "a"}, key="r"), 0.3)
    finally:
        m.stop()
    m = loop_machine(store, record=1.0)
    try:
        rec = m.submit({"n": 40, "tag": "a"}, key="r").result(10)
        assert rec.ok and rec.outputs == {"x": 40} and rec.stats.get("resumed")
        v = conform(m.code.entries["main"].region.job, rec, {"n": 40, "tag": "a"})
        assert v.ok, v.reason
    finally:
        m.stop()


def test_weak_digests_are_reported():
    prog = "@def (x: int)main(y):\n    (x)lock(lk)\n    (lk)ident(y)\n"
    m = machine(program=prog, bind={"lock": Inline(), "ident": Inline()}, record=1.0)
    try:
        rec = m.submit({"x": 1}).result(5)
        assert rec.stats["weak_digests"] == {"lock": 1}
    finally:
        m.stop()


def test_automatic_keys_are_warned_with_snapshots(caplog):
    m = loop_machine(MemoryStore())
    try:
        with caplog.at_level(logging.WARNING, logger="togeari.dispatcher"):
            m.run(Iterable([{"n": 2, "tag": "a"}]), Memory())
        assert any("keys are automatic" in r.message for r in caplog.records)
    finally:
        m.stop()


class Phis:
    """Stands in for the dispatcher: records what each phi resolves to and moves its versions."""

    def __init__(self):
        self.code = SimpleNamespace(mem=[])
        self.knobs = {}
        self.targets = []

    def resolve_phi(self, phi, target):
        self.targets.append(target)
        target.muts.extend(phi.muts)
        phi.muts = []


def test_each_unwritten_name_resolves_to_its_own_undefined_slot():
    d = Phis()
    fe = FrontEnd(d)
    for _ in range(2):
        phi = Slot(phi=True)
        phi.muts.append(((1,), Slot()))
        fe.resolve_join(Join({"x": phi}, None, False), NameMap())
    first, second = d.targets
    assert first is not second and first.value is UNDEF
    assert len(second.muts) == 1


class Prep:
    protocols = {"python"}
    light = True

    def __init__(self):
        self.barrier = threading.Barrier(2, timeout=2)

    def prepare(self, inputs, protocol):
        self.barrier.wait()
        return inputs

    def decode(self, raw, protocol):
        return raw


def test_payloads_of_different_items_build_at_once():
    a = Prep()
    items = [WorkItem("n", {"i": i}, a) for i in range(2)]
    errors = []

    def build(it):
        try:
            it.payload()
        except threading.BrokenBarrierError as exc:
            errors.append(exc)

    ts = [threading.Thread(target=build, args=(it,)) for it in items]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert not errors


class SlowPrep:
    protocols = {"python"}
    light = True

    def __init__(self):
        self.calls = 0

    def prepare(self, inputs, protocol):
        self.calls += 1
        time.sleep(0.05)
        return dict(inputs)

    def decode(self, raw, protocol):
        return raw


def test_one_items_payload_builds_once_under_concurrent_callers():
    a = SlowPrep()
    item = WorkItem("n", {"i": 1}, a)
    start = threading.Barrier(8, timeout=5)
    got = []

    def build():
        start.wait()
        got.append(item.payload())

    ts = [threading.Thread(target=build) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert a.calls == 1
    assert len(got) == 8 and all(p is got[0] for p in got)


class Box:
    def __init__(self, v):
        self.v = v


ZERO = "@def (xs: list)main(y):\n    (xs)zero(n)\n    (xs)ident(y)\n"


def test_positional_keys_digest_the_inputs_as_given():
    # The inputs digest is taken at job start, before `zero` changes xs in place: [5] and [7]
    # get two keys, though both read [0] by the positional statement
    store = MemoryStore()
    m = machine(
        program=ZERO,
        bind={"zero": Inline(), "ident": Use(sim_lane(), store="positional")},
        store=store,
    )
    try:
        first, second = [5], [7]
        assert m.submit({"xs": first}, key="k").result(5).outputs == {"y": [0]}
        assert first == [0]
        assert m.submit({"xs": second}, key="k").result(5).ok
        assert m.inspect("stats").result()["store_hits"] == 0 and len(store) == 2
    finally:
        m.stop()


class Spy:
    """A store that counts its calls and holds nothing."""

    def __init__(self):
        self.calls = 0

    def get(self, key):
        self.calls += 1
        return MISS

    def put(self, key, value):
        self.calls += 1


def test_weak_job_inputs_get_no_positional_key():
    # Inputs digested by type only make no positional key: the store is never asked
    spy = Spy()
    m = machine(
        program="@def (xs: list)main(y):\n    (xs)ident(y)\n",
        bind={"ident": Use(sim_lane(), store="positional")},
        store=spy,
    )
    try:
        assert m.submit({"xs": [threading.Lock()]}).result(5).ok
        assert spy.calls == 0 and m.inspect("stats").result()["store_weak"] == 1
    finally:
        m.stop()


class Copied:
    """Counts deep copies of its instances."""

    copies = 0

    def __deepcopy__(self, memo):
        Copied.copies += 1
        return Copied()


def test_snapshot_restore_keeps_the_unpickled_values():
    # Unpickled values are used as they are, never deep-copied again
    state = {
        "pc": 0,
        "prefix": (),
        "counter": 0,
        "frames": [],
        "values": {"v": Copied()},
        "stats": {},
        "steps": 0,
        "record": None,
    }
    job = Job("k", None, {})
    Copied.copies = 0
    assert snapshot.restore(job, pickle.dumps(state))
    (st,) = job.strands.values()
    assert Copied.copies == 0 and isinstance(st.names.get("v").value, Copied)


def test_registered_digests_are_used():
    register_digest(Box, lambda x: f"box{x.v}".encode())
    try:
        framed = b"r" + len(b"box1").to_bytes(8, "little") + b"box1"
        assert digest(Box(1)) == hashlib.sha256(framed).digest()
        assert digest(Box(1)) != digest(Box(2))
        registered = digest(Box(1))
    finally:
        unregister_digest(Box)

    # Unregistering also drops the cached lookup: the value digests without the function
    assert digest(Box(1)) != registered
