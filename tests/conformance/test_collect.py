"""Native ordered collection, private retirement state, faults and fallback replay."""

import gc
import threading

import pytest
from kohakunode import NodeLibrary
from kohakunode.sig.std import INTERNAL

from togeari import ConstructionError, Inline, Machine, Use
from togeari.cli.replay import replay_dir
from togeari.conformance import conform
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.results import Cancelled, ItemError, Ok
from togeari.plugins import Recorder
from togeari.stores import MemoryStore
from togeari.testing import SimCall

LIB = NodeLibrary("collecttest")
SEEN = []


@LIB.node()
def fail_odd(x: int) -> int:
    if x % 2:
        raise ValueError(f"odd {x}")
    return x * 10


@LIB.node(ordered=True)
def note(x: int) -> int:
    SEEN.append(x)
    return x


@LIB.node()
def collect_gc(x: int) -> int:
    gc.collect()
    return x


class ReverseReplies(EngineBase):
    """Hold three iterations then complete newest first, without a timing assertion."""

    protocol = "python"

    def __init__(self):
        super().__init__("reverse")
        self.pending = []
        self.order = []

    def capacity(self):
        return 3

    def submit(self, item, done):
        self.pending.append((item, done))
        if len(self.pending) == 3:
            pending, self.pending = self.pending, []
            for work, reply in reversed(pending):
                self.order.append(work.inputs["x"])
                try:
                    result = Ok(work.payload().run(), True)
                except Exception as exc:  # noqa: BLE001
                    result = ItemError(exc)
                reply(work.key, [result])
        return item.key


BASIC = """@def (xs: list)main(out):
    (xs)collect(x, i, `body`, y -> out)
    body:
        (x, i)add(y)
"""
NESTED = """@def (xs: list)main(out):
    (xs)collect(x, `outer`, inner -> out)
    outer:
        ([1, 2])collect(y, `body`, z -> inner)
        body:
            (x, y)add(z)
"""
CARRIED = """@def (xs: list)main(out, total):
    total = 0
    (xs)collect(x, `body`, total -> out)
    body:
        (total, x)add(total)
"""
LOCAL_CATCH = """@def (xs: list)main(out):
    (xs)collect(x, `body`, value -> out)
    body:
        @try:
            (x)fail_odd(value)
        @except error:
            value = -1
"""
OUTER_CATCH = """@def (xs: list)main(out, last):
    out = [99]
    last = -1
    @try:
        (xs)collect(x, `body`, value -> out)
        body:
            last = x
            (x)note(_)
            (x)fail_odd(value)
    @except error:
        last = last
"""
NESTED_CATCH = """@def (xs: list)main(out):
    (xs)collect(x, `outer`, inner -> out)
    outer:
        inner = [99]
        @try:
            ([0, 1, 2])collect(y, `body`, z -> inner)
            body:
                (y)fail_odd(z)
        @except error:
            inner = [-1]
"""


def run(program, inputs, core, *, passes=None, lane=False, **knobs):
    bind = {"*": Use(SimCall(name="work", latency_ms=0.2))} if lane else {"*": Inline()}
    m = Machine(
        program=program,
        entry="main",
        libraries=[LIB],
        bind=bind,
        core=core,
        record=1.0,
        passes=passes,
        **knobs,
    )
    try:
        record = m.submit(inputs, key="collect").result(10)
        job = m.code.entries["main"].region.job
        gc.collect()
    finally:
        m.stop()
    return m, job, record


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("lane", [False, True])
@pytest.mark.parametrize(
    "program,inputs,expected",
    [
        (BASIC, {"xs": []}, {"out": []}),
        (BASIC, {"xs": [3, 2, 1]}, {"out": [3, 3, 3]}),
        (NESTED, {"xs": [10, 20]}, {"out": [[11, 12], [21, 22]]}),
        (CARRIED, {"xs": [1, 2, 3]}, {"out": [1, 3, 6], "total": 6}),
        (LOCAL_CATCH, {"xs": [0, 1, 2, 3]}, {"out": [0, -1, 20, -1]}),
        (NESTED_CATCH, {"xs": [10, 20]}, {"out": [[-1], [-1]]}),
    ],
)
def test_collect_outputs_and_reference(program, inputs, expected, core, lane):
    _, job, record = run(program, inputs, core, lane=lane)
    assert record.ok, record.failure
    assert record.outputs == expected
    verdict = conform(job, record, inputs)
    assert verdict.ok, verdict
    assert all(not call[0].startswith("kir.collect") for call in record.record)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_collect_fault_keeps_prior_result_and_earlier_effects(core):
    SEEN.clear()
    _, job, record = run(OUTER_CATCH, {"xs": [0, 1, 2]}, core)
    assert record.ok, record.failure
    assert record.outputs == {"out": [99], "last": 1}
    assert SEEN == [0, 1]
    assert conform(job, record, {"xs": [0, 1, 2]}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_collect_retains_mutable_values_without_copying_them(core):
    text = """@def (xs: list)main(out):
    (xs)collect(x, `body`, x -> out)
    body:
"""
    shared = [1, 2]
    _, job, record = run(text, {"xs": [shared, shared]}, core)
    assert record.ok and record.outputs == {"out": [[1, 2], [1, 2]]}
    assert record.outputs["out"][0] is record.outputs["out"][1]
    assert conform(job, record, {"xs": [shared, shared]}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_collect_large_ordered_result_is_not_recorded_as_admin_calls(core):
    text = """@def (xs: list)main(out):
    (xs)collect(x, `body`, x -> out)
    body:
"""
    _, job, record = run(text, {"xs": list(range(2000))}, core, window=8)
    assert record.ok and record.outputs["out"] == list(range(2000))
    assert record.record == [] and record.stats["calls"] == 0
    assert record.stats["collect_captures"] == 2000
    assert record.stats["collect_peak_items"] == 2000
    assert record.stats["collect_finalizations"] == 1
    assert record.stats["bytes_peak"] >= 2000 * 16
    assert conform(job, record, {"xs": list(range(2000))}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize(
    "passes", [None, ["order_dataflow", "expand_collect", "inline", "strip_meta"]]
)
def test_collect_archive_and_fallback(tmp_path, core, passes):
    recorder = Recorder(tmp_path, libraries=["test_collect:LIB"])
    _, job, record = run(NESTED, {"xs": [1, 2]}, core, passes=passes, plugins=[recorder])
    assert record.ok and record.outputs == {"out": [[2, 3], [3, 4]]}
    assert conform(job, record, {"xs": [1, 2]}).ok
    [replayed] = list(replay_dir(tmp_path))
    assert replayed.verdict.ok, replayed
    if passes:
        assert any(call[0] == "kir.collect_append" for call in record.record)


class SnapshotLog(MemoryStore):
    def __init__(self):
        super().__init__()
        self.written = []

    def put(self, key, value):
        self.written.append(value)
        super().put(key, value)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_active_collectors_do_not_write_partial_snapshots(core):
    store = SnapshotLog()
    _, _, record = run(LOCAL_CATCH, {"xs": [0, 1, 2]}, core, store=store, snapshot_every=1)
    assert record.ok and not record.stats.get("snapshot_error")
    assert len(store) == 0 and store.written == []


class BrokenItems:
    def __iter__(self):
        raise ValueError("cannot materialize items")


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("xs,kind", [(4, "TypeError"), (BrokenItems(), "ValueError")])
@pytest.mark.parametrize(
    "passes", [None, ["order_dataflow", "expand_collect", "inline", "strip_meta"]]
)
def test_collect_input_snapshot_error_is_catchable(core, passes, xs, kind):
    text = """@def (xs)main(out, error):
    out = [99]
    error = None
    @try:
        (xs)collect(x, `body`, x -> out)
        body:
    @except caught:
        error = caught
"""
    _, job, record = run(text, {"xs": xs}, core, passes=passes)
    assert record.ok and record.outputs["out"] == [99], record.failure
    assert record.outputs["error"]["kind"] == kind
    assert record.outputs["error"]["node"] == "kir.collect_items"
    assert conform(job, record, {"xs": xs}).ok
    assert len(record.record) == (1 if passes else 0)


class DeferredInput(EngineBase):
    protocol = "python"

    def __init__(self):
        super().__init__("deferred-input")
        self.entered = threading.Event()
        self.reply = None

    def capacity(self):
        return 1

    def submit(self, item, done):
        self.reply = done
        self.entered.set()
        return "input"


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("caught", [False, True])
def test_parked_collect_input_error_preserves_target_and_squashes_ordered_after(core, caught):
    header = """@def ()main(out, after, error):
    out = [99]
    after = -1
    error = None
    (4)identity(items)
"""
    body = """(items)collect(x, `body`, x -> out)
body:
(77)note(after)
"""
    indent = "        " if caught else "    "
    text = header + ("    @try:\n" if caught else "")
    text += "\n".join(indent + line for line in body.splitlines()) + "\n"
    if caught:
        text += "    @except failure:\n        error = failure\n"
    SEEN.clear()
    engine = DeferredInput()
    faults = []
    m = Machine(
        program=text,
        entry="main",
        libraries=[LIB],
        bind={"identity": Use(engine), "note": Inline()},
        core=core,
        record=1.0,
    )
    m.bus.listen(lambda kind, fields: faults.append(fields), kinds=["fault.taken"])
    try:
        handle = m.submit({}, key="parked-error")
        assert engine.entered.wait(5)
        [resident] = m.inspect("jobs").result(5)
        view = m.inspect("job", id=resident["id"]).result(5)
        assert view["stats"]["parks"] >= 1
        engine.reply("input", [Ok(4, True)])
        record = handle.result(5)
        job = m.code.entries["main"].region.job
    finally:
        m.stop()
    assert record.ok == caught
    assert record.outputs["out"] == [99] and record.outputs["after"] == -1
    assert SEEN == []
    error = record.outputs["error"] if caught else record.failure
    assert error["kind"] == "TypeError" and error["node"] == "kir.collect_items"
    assert conform(job, record, {}).ok
    assert [event["key"] for event in faults] == [(4,)]


@pytest.mark.parametrize("core", ["python", "rust"])
def test_collect_parked_input_and_branch_yields(core):
    text = """@def (xs: list)main(out, after):
    (xs)identity(items)
    (items)collect(x, `body`, value -> out)
    body:
        (x, 2)less(small)
        (small)branch(`a`, `b`)
        a:
            (x, 10)add(value)
        b:
            (x, 20)add(value)
    (out)len(after)
"""
    _, job, record = run(text, {"xs": [0, 1, 2, 3]}, core, lane=True)
    assert record.ok and record.outputs == {"out": [10, 11, 22, 23], "after": 4}
    assert conform(job, record, {"xs": [0, 1, 2, 3]}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
@pytest.mark.parametrize("xs,success", [([0, 2, 4], True), ([1, 2, 4], False)])
def test_retirement_order_and_ordered_after_delayed_fault(core, xs, success):
    text = """@def (xs: list)main(out, after):
    out = [99]
    (xs)collect(x, `body`, value -> out)
    body:
        (x)fail_odd(value)
    (77)note(after)
"""
    SEEN.clear()
    engine = ReverseReplies()
    m = Machine(
        program=text,
        entry="main",
        libraries=[LIB],
        bind={"fail_odd": Use(engine), "note": Inline()},
        core=core,
        record=1.0,
    )
    try:
        record = m.submit({"xs": xs}, key="ordered").result(5)
        job = m.code.entries["main"].region.job
    finally:
        m.stop()
    assert engine.order == list(reversed(xs))
    assert record.ok == success
    assert record.outputs["out"] == ([0, 20, 40] if success else [99])
    assert ([77] if success else []) == SEEN
    assert conform(job, record, {"xs": xs}).ok
    assert record.stats.get("collect_captures", 0) == (3 if success else 0)


@pytest.mark.parametrize("core", ["python", "rust"])
def test_nested_collect_small_window_local_recovery_and_gc(core):
    _, job, record = run(
        NESTED_CATCH,
        {"xs": [10, 20, 30]},
        core,
        window=1,
        retire_buffer=1,
        snapshot_every=1,
        store=MemoryStore(),
    )
    assert record.ok and record.outputs == {"out": [[-1], [-1], [-1]]}
    assert conform(job, record, {"xs": [10, 20, 30]}).ok
    text = """@def (xs: list)main(out):
    (xs)collect(x, `body`, value -> out)
    body:
        (x)collect_gc(value)
"""
    _, job, record = run(text, {"xs": [1, 2, 3]}, core)
    assert record.ok and record.outputs == {"out": [1, 2, 3]}
    assert conform(job, record, {"xs": [1, 2, 3]}).ok


@pytest.mark.parametrize("core", ["python", "rust"])
def test_collect_without_recording(core):
    m = Machine(program=NESTED, entry="main", bind={"*": Inline()}, core=core, record=None)
    try:
        record = m.submit({"xs": [1, 2]}).result(5)
        assert record.ok and record.outputs == {"out": [[2, 3], [3, 4]]}
        assert record.record is None
        assert record.stats["collect_captures"] == 6
        assert record.stats["collect_finalizations"] == 3
    finally:
        m.stop()


class PendingReply(EngineBase):
    protocol = "python"

    def __init__(self):
        super().__init__("pending")
        self.entered = threading.Event()
        self.pending = {}

    def capacity(self):
        return 8

    def submit(self, item, done):
        ticket = id(item)
        if item.inputs["x"] == 2:
            self.pending[ticket] = done
            self.entered.set()
        else:
            done(ticket, [Ok(item.payload().run(), True)])
        return ticket

    def cancel(self, ticket):
        reply = self.pending.pop(ticket, None)
        if reply is not None:
            reply(ticket, [Cancelled()])


@pytest.mark.parametrize("core", ["python", "rust"])
def test_cancel_nested_collectors_releases_private_state(core):
    text = """@def ()main(out):
    ([0, 1])collect(outer, `a`, inner -> out)
    a:
        ([0, 2])collect(x, `b`, value -> inner)
        b:
            (x)fail_odd(value)
"""
    engine = PendingReply()
    m = Machine(
        program=text,
        entry="main",
        libraries=[LIB],
        bind={"fail_odd": Use(engine)},
        core=core,
        record=1.0,
    )
    try:
        handle = m.submit({}, key="cancel")
        assert engine.entered.wait(5)
        assert m.inspect("jobs").result(5)
        gc.collect()
    finally:
        m.stop(drain=False, timeout=5)
    record = handle.result(5)
    assert not record.ok and record.failure["kind"] == "Cancelled"
    assert "out" not in record.outputs
    assert not engine.pending


@pytest.mark.parametrize("core", ["python", "rust"])
def test_native_linear_capture_avoids_baseline_reference_copying(core, monkeypatch):
    copied = []
    original = INTERNAL.impls["kir.collect_append"]

    def counted(items, item):
        copied.append(len(items) + 1)
        return original(items, item)

    monkeypatch.setitem(INTERNAL.impls, "kir.collect_append", counted)
    text = """@def (xs: list)main(out):
    (xs)collect(x, `body`, x -> out)
    body:
"""
    n = 257
    inputs = {"xs": list(range(n))}
    _, _, native = run(text, inputs, core, window=8, retire_buffer=8)
    assert native.ok and native.outputs["out"] == inputs["xs"]
    assert copied == []
    assert native.stats["collect_captures"] == native.stats["collect_peak_items"] == n
    _, _, expanded = run(
        text, inputs, core, passes=["order_dataflow", "expand_collect", "inline", "strip_meta"]
    )
    assert expanded.ok and expanded.outputs == native.outputs
    assert len(copied) == n and sum(copied) == n * (n + 1) // 2


@pytest.mark.parametrize("core", ["python", "rust"])
def test_machine_rejects_declared_mutation_anywhere_with_collect(core):
    lib = NodeLibrary("unsafe_collect")

    @lib.node(mutates=["items"])
    def mutate(items: list) -> list:
        items.append(0)
        return items

    text = BASIC + "\n@def (items: list)unused(out):\n    (items)mutate(out)\n"
    with pytest.raises(ConstructionError, match="mutat"):
        Machine(program=text, entry="main", libraries=[lib], bind={"*": Inline()}, core=core)
