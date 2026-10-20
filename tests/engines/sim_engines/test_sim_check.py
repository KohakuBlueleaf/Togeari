"""check_engine on the simulated engines, plus engines that break a rule on purpose."""

import threading

import pytest

from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.results import Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.common.errors import EngineStopped
from togeari.testing import SimBatch, SimCall, SimStream, check_engine


def triple(x):
    return 3 * x


def broken(x):
    raise ValueError(f"broken on {x}")


def samples(n=8):
    return [WorkItem("triple", {"x": i}, PythonAdapter(triple)) for i in range(n)]


def failing():
    return [WorkItem("broken", {"x": 1}, PythonAdapter(broken))]


def outage(engine):
    engine.outages.append((0.0, 1e9))


@pytest.mark.parametrize("make", [SimCall, SimBatch, SimStream], ids=["call", "batch", "stream"])
def test_sim_engines_keep_every_rule(make):
    engine = make(latency_ms=0.5, seed=3)
    report = check_engine(
        lambda: engine, samples(), failing_items=failing(), kill=lambda: outage(engine)
    )
    assert report.ok, str(report)
    assert [r.rule for r in report.rules] == ["B1", "B2", "B3", "B4", "B5", "B6", "B7"]
    b6 = report.rule("B6")
    assert b6.ok and b6.detail.startswith("n/a") is (make is not SimBatch)


class _Doubler(EngineBase):
    """Answers every item twice from a thread: breaks B2."""

    kind = "call"

    def capacity(self):
        return 4

    def submit(self, work, done):
        def run():
            r = [Ok(work.payload().run(), True)]
            done(None, r)
            done(None, r)

        threading.Thread(target=run, daemon=True).start()
        return None


class _Inline(EngineBase):
    """Runs the work inside submit: breaks B1."""

    kind = "call"

    def capacity(self):
        return 4

    def submit(self, work, done):
        done(None, [Ok(work.payload().run(), True)])
        return None


class _Threaded(EngineBase):
    """Runs each item on a thread of its own; the subclasses break B3."""

    kind = "call"

    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.running = 0

    def capacity(self):
        with self.lock:
            return 4 - self.running

    def submit(self, work, done):
        with self.lock:
            self.running += 1
        threading.Thread(target=self.run, args=(work, done), daemon=True).start()
        return None

    def run(self, work, done):
        value = work.payload().run()
        with self.lock:
            self.running -= 1
        done(None, [Ok(value, True)])


class _LockedDone(_Threaded):
    """Calls ``done`` while holding the lock ``capacity()`` takes."""

    def run(self, work, done):
        value = work.payload().run()
        with self.lock:
            self.running -= 1
            done(None, [Ok(value, True)])


class _SelfCalling(_Threaded):
    """Calls its own ``capacity()`` from its worker thread."""

    def run(self, work, done):
        self.capacity()
        super().run(work, done)


class Heavy(PythonAdapter):
    light = False


class _KeyPrepares(EngineBase):
    """A batch engine whose ``key`` builds the payload, on the caller's thread."""

    kind = "batch"
    max_batch = 4

    def capacity(self):
        return 4

    def key(self, item):
        return type(item.payload()).__name__

    def submit(self, work, done):
        def run():
            done(None, [Ok(it.payload().run(), True) for it in work])

        threading.Thread(target=run, daemon=True).start()
        return None


def heavy_samples(n=4):
    return [WorkItem("triple", {"x": i}, Heavy(triple)) for i in range(n)]


def test_b3_passes_a_well_behaved_threaded_engine():
    report = check_engine(_Threaded, samples(4), rounds=20, grace_s=0.0, probe_s=0.2)
    assert report.rule("B3").ok and report.rule("B2").ok, str(report)


@pytest.mark.parametrize("engine", [_LockedDone, _SelfCalling], ids=["locked-done", "self-call"])
def test_check_catches_threading_bugs(engine):
    report = check_engine(engine, samples(4), rounds=20, grace_s=0.0, probe_s=0.2)
    assert report.rule("B3").ok is False, str(report)


def test_check_catches_a_heavy_prepare_on_the_caller():
    report = check_engine(_KeyPrepares, heavy_samples(), rounds=20, grace_s=0.0, probe_s=0.2)
    assert report.rule("B3").ok is False, str(report)
    assert "heavy-adapter prepare" in report.rule("B3").detail

    # The same engine with a light adapter keeps B3.
    report = check_engine(_KeyPrepares, samples(4), rounds=20, grace_s=0.0, probe_s=0.2)
    assert report.rule("B3").ok, str(report)


def test_check_catches_duplicate_answers():
    report = check_engine(_Doubler, samples(4), rounds=20, grace_s=0.1)
    assert report.rule("B2").ok is False and not report.ok, str(report)


def test_check_catches_work_inside_submit():
    report = check_engine(_Inline, samples(4), rounds=20, grace_s=0.0)
    assert report.rule("B1").ok is False and not report.ok, str(report)
    assert "Ok results delivered inside submit" in report.rule("B1").detail

    # A well-behaved engine reports none
    report = check_engine(_Threaded, samples(4), rounds=20, grace_s=0.0)
    assert "0 Ok results delivered inside submit" in report.rule("B1").detail


@pytest.mark.parametrize("make", [SimCall, SimBatch, SimStream], ids=["call", "batch", "stream"])
def test_sim_stop_answers_pending_tickets(make):
    engine = make(latency_ms=60_000)
    engine.start()
    got = []
    answered = threading.Event()
    engine.submit(
        samples(2) if make is SimBatch else samples(1)[0],
        lambda t, r: (got.extend(r), answered.set()),
    )
    engine.stop()
    assert answered.wait(5.0), "stop left a ticket unanswered"
    assert got and all(isinstance(r, UnitError) for r in got), got
    assert engine.load().running == 0


@pytest.mark.parametrize("make", [SimCall, SimBatch, SimStream], ids=["call", "batch", "stream"])
def test_sim_submit_after_stop_answers_engine_stopped(make):
    engine = make(latency_ms=1.0)
    engine.start()
    engine.stop()
    got = []
    work = samples(3) if make is SimBatch else samples(1)[0]
    ticket = engine.submit(work, lambda t, r: got.extend(r))

    # Answered at once, every item, nothing left in flight
    assert ticket == -1
    assert len(got) == (3 if make is SimBatch else 1)
    assert all(isinstance(r, UnitError) and isinstance(r.error, EngineStopped) for r in got)
    assert engine.load().running == 0 and engine.capacity() == engine.cap
