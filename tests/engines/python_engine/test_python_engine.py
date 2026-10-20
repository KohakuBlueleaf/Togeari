"""PythonCallEngine: behaviour checks in both modes, worker death, cancel, the shm transport."""

import hashlib
import random
import threading
import time

import pyfuncs
import pyprobe
import pytest

from togeari.core.contracts.adapter import PythonAdapter
from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.common.errors import WorkerDied
from togeari.engines.python import PythonCallEngine
from togeari.testing import check_engine


def item(fn, **inputs):
    return WorkItem(fn.__name__, inputs, PythonAdapter(fn))


def run_one(engine, work, timeout=30.0):
    """Submit one item and wait for its result."""
    got = []
    event = threading.Event()

    def done(ticket, results):
        got.extend(results)
        event.set()

    engine.submit(work, done)
    assert event.wait(timeout), "no answer"
    return got[0]


def until(cond, timeout=30.0):
    """Wait for ``cond()``; its last value."""
    deadline = time.monotonic() + timeout
    while not (value := cond()) and time.monotonic() < deadline:
        time.sleep(0.01)
    return value


def kill_workers(engine):
    """Terminate every worker process, the way a crash does, once no slot is busy restarting."""
    assert until(lambda: engine.load().running == 0)
    for slot in list(engine.slots):
        slot.proc.kill()


@pytest.mark.parametrize(
    "mode, transport", [("thread", "pickle"), ("process", "pickle"), ("process", "shm")]
)
def test_python_engine_keeps_every_rule(mode, transport):
    engine = PythonCallEngine(workers=4, mode=mode, transport=transport)

    # Process mode: the worker processes are terminated; thread mode has no process to kill.
    report = check_engine(
        lambda: engine,
        [item(pyfuncs.double, x=i) for i in range(8)],
        failing_items=[item(pyfuncs.fail, x=1)],
        kill=(lambda: kill_workers(engine)) if mode == "process" else engine.stop,
        rounds=150,
    )
    assert report.ok, str(report)


@pytest.mark.parametrize("mode", ["thread", "process"])
def test_a_slot_runs_items_across_restarts(mode):
    engine = PythonCallEngine(workers=1, mode=mode)
    try:
        for start in range(2):
            engine.start()
            assert run_one(engine, item(pyfuncs.double, x=start), timeout=30.0) == Ok(
                2 * start, True
            )
            engine.stop()
    finally:
        engine.stop()


@pytest.mark.parametrize("mode", ["thread", "process"])
def test_spec_is_identity_only(mode):
    engine = PythonCallEngine(workers=2, mode=mode)
    before = engine.spec()
    engine.start()
    try:
        run_one(engine, item(pyfuncs.double, x=1))
        after = engine.spec()
    finally:
        engine.stop()
    assert after == before


def test_cancel_kills_a_hung_worker_and_restarts_it():
    engine = PythonCallEngine(workers=1, mode="process")
    engine.start()
    try:
        got = []
        ticket = engine.submit(item(pyfuncs.nap, x=1, ms=600_000), lambda t, r: got.extend(r))
        assert until(lambda: engine.load().running == 1)
        assert engine.capacity() == 0

        # The cancel answers at once; the slot stays busy until its new process is up.
        engine.cancel(ticket)
        assert got == [Cancelled()]
        assert engine.capacity() == 0
        assert until(lambda: engine.capacity() == 1)
        assert engine.ready()
        assert run_one(engine, item(pyfuncs.double, x=4)) == Ok(8, True)
        assert engine.load().extra["kills"] == 1
    finally:
        engine.stop()


def test_cancel_in_thread_mode_keeps_the_slot_busy_until_return():
    engine = PythonCallEngine(workers=1, mode="thread")
    engine.start()
    try:
        returned = threading.Event()
        ticket = engine.submit(item(pyfuncs.nap, x=1, ms=300), lambda t, r: returned.set())
        assert until(lambda: engine.load().running == 1)
        engine.cancel(ticket)
        assert engine.capacity() == 0
        assert until(lambda: engine.capacity() == 1, timeout=5.0)
    finally:
        engine.stop()


def test_a_restart_starts_with_free_slots_while_an_old_item_still_runs():
    engine = PythonCallEngine(workers=1, mode="thread", stop_timeout_s=0.05)
    pyfuncs.GATE.clear()
    engine.start()
    old = []
    try:
        engine.submit(item(pyfuncs.gated, x=1), lambda t, r: old.extend(r))
        assert until(lambda: engine.load().running == 1)
        [old_slot] = engine.slots
        engine.stop()
        assert len(old) == 1 and isinstance(old[0], UnitError)

        # The new run's slot is free although the old one still runs its item
        engine.start()
        assert engine.capacity() == 1 and engine.load().running == 0
        assert run_one(engine, item(pyfuncs.double, x=3)) == Ok(6, True)

        # The old item's end changes nothing of the new run
        pyfuncs.GATE.set()
        old_slot.thread.join(10.0)
        assert not old_slot.thread.is_alive()
        assert engine.capacity() == 1 and engine.load().running == 0
    finally:
        pyfuncs.GATE.set()
        engine.stop()
        pyfuncs.GATE.clear()


def test_cancel_of_a_waiting_item_frees_its_place():
    engine = PythonCallEngine(workers=1, mode="thread")
    engine.start()
    try:
        engine.submit(item(pyfuncs.nap, x=1, ms=300), lambda t, r: None)
        assert until(lambda: engine.load().running == 1)
        waiting = engine.submit(item(pyfuncs.double, x=1), lambda t, r: None)
        assert engine.load().waiting == 1
        engine.cancel(waiting)
        assert engine.load().waiting == 0
    finally:
        engine.stop()


def test_an_idle_worker_death_takes_its_slot_out_until_replaced():
    engine = PythonCallEngine(workers=2, mode="process")
    engine.start()
    try:
        engine.slots[0].proc.kill()
        assert until(lambda: engine.capacity() == 1, timeout=10.0)
        assert until(lambda: engine.capacity() == 2)

        # No item meets the dead process.
        results = [run_one(engine, item(pyfuncs.double, x=i)) for i in range(4)]
        assert results == [Ok(2 * i, True) for i in range(4)]
    finally:
        engine.stop()


def test_every_worker_dead_is_a_unit_error_until_ready_revives():
    engine = PythonCallEngine(workers=2, mode="process")
    engine.start()
    try:
        kill_workers(engine)
        assert until(lambda: engine.load().extra["state"] == "down", timeout=10.0)
        assert isinstance(run_one(engine, item(pyfuncs.double, x=1)), UnitError)
        assert until(engine.ready)
        assert run_one(engine, item(pyfuncs.double, x=2)) == Ok(4, True)
    finally:
        engine.stop()


def test_dead_worker_fails_its_item_and_restarts():
    engine = PythonCallEngine(workers=1, mode="process")
    engine.start()
    try:
        r = run_one(engine, item(pyfuncs.die, x=0))
        assert isinstance(r, ItemError) and isinstance(r.error, WorkerDied), r
        assert run_one(engine, item(pyfuncs.double, x=21)) == Ok(42, True)
        assert engine.load().extra["restarts"] == 1
        assert engine.capacity() == 1
    finally:
        engine.stop()


@pytest.mark.parametrize("transport", ["pickle", "shm"])
def test_large_buffers_cross_by_the_chosen_transport(transport):
    np = pytest.importorskip("numpy")
    blob = np.frombuffer(random.Random(0).randbytes(1 << 20), dtype=np.uint8).copy()
    small = bytearray(b"in band")
    engine = PythonCallEngine(workers=1, mode="process", transport=transport)
    engine.start()
    try:
        r = run_one(engine, item(pyfuncs.echo, blob=(blob, small)))
        shm_bytes = engine.load().extra["shm_bytes"]
    finally:
        engine.stop()
    assert isinstance(r, Ok)
    out, back = r.value
    assert hashlib.sha256(out.tobytes()).hexdigest() == hashlib.sha256(blob.tobytes()).hexdigest()
    assert back == small
    # The array crosses both ways through shared memory with "shm", never with "pickle".
    assert shm_bytes == (2 * blob.nbytes if transport == "shm" else 0)


@pytest.mark.parametrize("preload", [True, False])
def test_workers_import_libraries_at_start(preload):
    engine = PythonCallEngine(workers=1, mode="process", imports=("pyfuncs",) if preload else ())
    engine.start()
    try:
        # The first call imports only `pyprobe`: `pyfuncs` is there only if preloaded
        r = run_one(engine, item(pyprobe.imported, name="pyfuncs"))
        after = run_one(engine, item(pyfuncs.double, x=2))
        again = run_one(engine, item(pyprobe.imported, name="pyfuncs"))
    finally:
        engine.stop()
    assert r == Ok(preload, True)
    assert after == Ok(4, True) and again == Ok(True, True)


def test_capacity_counts_free_workers():
    engine = PythonCallEngine(workers=3, mode="thread")
    engine.start()
    try:
        assert engine.capacity() == 3
        gate = threading.Event()
        engine.submit(item(pyfuncs.nap, x=1, ms=300), lambda t, r: gate.set())
        assert engine.capacity() == 2
        assert gate.wait(5.0)
        assert engine.capacity() == 3
    finally:
        engine.stop()
