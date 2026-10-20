"""RunReport's writer under floods and at unload, emits from plugin threads, job-state cost."""

import json
import pathlib
import threading
import time
from types import SimpleNamespace

from kohakunode import NodeLibrary

from togeari import BasePlugin, Inline, Machine
from togeari.core.observe.bus import Event
from togeari.core.plugin.base import job_state
from togeari.core.runtime.state import InfState
from togeari.plugins import RunReport

LIB = NodeLibrary("writer")


@LIB.node()
def inc(x: int) -> int:
    return x + 1


INC = "@def (x: int)main(y):\n    (x)inc(y)\n"
EV = Event("probe", 0.0, {"n": 1})


def wait_for(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class SlowFile:
    """A text sink whose writes take ``delay_s`` each, after ``gate`` opens."""

    def __init__(self, delay_s: float, gate: threading.Event | None = None):
        self.delay_s = delay_s
        self.gate = gate
        self.lines = 0
        self.closed = False

    def write(self, line: str) -> None:
        if self.closed:
            raise ValueError("write to a closed file")
        if self.gate is not None:
            self.gate.wait(10)
        time.sleep(self.delay_s)
        self.lines += 1

    def flush(self) -> None:
        if self.closed:
            raise ValueError("flush of a closed file")

    def close(self) -> None:
        self.closed = True


def report_machine(report: RunReport) -> Machine:
    return Machine(
        program=INC, entry="main", libraries=[LIB], bind={"inc": Inline()}, plugins=[report]
    )


# ---------------------------------------------------------------- summary under a flood
def test_a_summary_taken_while_the_writer_queue_is_full_is_still_written(tmp_path):
    out = tmp_path / "report"
    report = RunReport(out, compress=False, interval_s=0.05)
    report.queue_size = 20
    m = report_machine(report)
    gate = threading.Event()
    try:
        assert m.submit({"x": 1}).result(5).ok
        watchers = report.counters.watchers
        watchers.remove(report._on_counters)

        # The writer blocks on its first event and the queue fills behind it
        report._events = SlowFile(0.0, gate)
        worker = report.worker
        while worker.put(("event", EV)):
            pass
        (out / "summary.json").unlink(missing_ok=True)

        # Snapshots arrive only while the queue is full
        mark = time.time()
        watchers.append(report._on_counters)
        assert wait_for(lambda: report.last.get("t", 0) > mark, timeout=2)
        time.sleep(0.3)
        watchers.remove(report._on_counters)

        # Released, the writer drains the queue; the summary taken while full is written
        gate.set()
        assert wait_for(lambda: worker.q.qsize() == 0, timeout=5)
        assert wait_for(lambda: (out / "summary.json").exists(), timeout=2)
        early = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert early["writer_dropped"] > 0
    finally:
        gate.set()
        m.stop()


# ---------------------------------------------------------------- unload
def test_unload_stops_a_slow_writer_within_close_s_and_nothing_writes_after(tmp_path):
    out = tmp_path / "report"
    report = RunReport(out, compress=False, interval_s=10.0, close_s=0.2)
    m = report_machine(report)
    assert m.submit({"x": 1}).result(5).ok
    slow = SlowFile(0.05)
    report._events = slow
    worker = report.worker
    for _ in range(200):
        worker.put(("event", EV))

    # 200 writes of 50 ms would take 10 s; unload returns after about two close_s
    t0 = time.monotonic()
    m.stop()
    assert time.monotonic() - t0 < 2.0
    assert wait_for(lambda: not worker.thread.is_alive(), timeout=2)
    assert slow.closed and slow.lines < 200

    # A late task and a late summary write nothing and raise nothing
    final = (out / "summary.json").read_text(encoding="utf-8")
    report._pending = {"late": True}
    report._handle(("event", EV))
    report._flush_maybe()
    assert (out / "summary.json").read_text(encoding="utf-8") == final
    assert not list(out.glob("*.tmp"))
    assert json.loads(final)["jobs"] == 1


def test_a_replace_refused_by_a_reader_is_retried(tmp_path, monkeypatch):
    report = RunReport(tmp_path / "report", compress=False)
    report.path.mkdir(parents=True)
    real = pathlib.Path.replace
    refusals = []

    def refusing(self, target):
        if len(refusals) < 2:
            refusals.append(target)
            raise PermissionError("held open by a reader")
        return real(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", refusing)
    report._write_json("summary.json", {"n": 1})
    assert len(refusals) == 2
    assert json.loads((report.path / "summary.json").read_text(encoding="utf-8")) == {"n": 1}


# ---------------------------------------------------------------- emits from plugin threads
class Emitter(BasePlugin):
    name = "emitter"

    def on_load(self, ctx):
        self.ctx = ctx


def test_an_emit_from_a_plugin_thread_runs_on_the_dispatcher_thread():
    plugin = Emitter()
    m = Machine(
        program=INC, entry="main", libraries=[LIB], bind={"inc": Inline()}, plugins=[plugin]
    )
    seen = []
    m.bus.listen(lambda kind, fields: seen.append(threading.current_thread()), kinds=["probe"])
    try:
        assert m.submit({"x": 1}).result(5).ok
        t = threading.Thread(target=lambda: plugin.ctx.emit("probe", n=1))
        t.start()
        t.join(5)
        assert wait_for(lambda: len(seen) == 1)
        assert seen == [m.dispatcher.thread]
        assert plugin.ctx.event_counts().get("probe") == 1
    finally:
        m.stop()


# ---------------------------------------------------------------- job_state cost
class Untouchable:
    """A window entry the scan must not reach."""

    @property
    def state(self):
        raise AssertionError("scanned past the last issued call")


def test_job_state_scan_ends_at_the_last_issued_call():
    queued = SimpleNamespace(state=InfState.ISSUED, item=SimpleNamespace(ext={}))
    job = SimpleNamespace(
        paused=False,
        issued=1,
        window=[queued, Untouchable(), Untouchable()],
        unfinished=1,
        runnable=[],
        strands={},
    )
    assert job_state(job) == "queued"
