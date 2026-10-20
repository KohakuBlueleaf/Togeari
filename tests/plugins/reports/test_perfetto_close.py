"""Perfetto's unload: bounded by close_s; the trace array closes and nothing writes after it."""

import time

from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari.plugins import Perfetto


def test_unload_stops_a_slow_writer_within_close_s(tmp_path):
    path = tmp_path / "trace.json"
    trace = Perfetto(path, sample_s=10.0, close_s=0.2)
    m = build(PROGRAM, "main", plugins=[trace])
    run_jobs(m, jobs(2))
    worker = trace.worker

    # Every queued task now takes 50 ms: 400 of them would take 20 s
    def slow(task):
        time.sleep(0.05)

    trace._write_task = slow
    for _ in range(400):
        worker.put(("raw", {"ph": "M"}))
    t0 = time.monotonic()
    m.stop()
    assert time.monotonic() - t0 < 5.0

    # The array is closed and stays closed
    text = path.read_text(encoding="utf-8")
    assert text.rstrip().endswith("]")
    time.sleep(0.3)
    assert path.read_text(encoding="utf-8") == text


def test_a_writer_of_an_earlier_load_writes_nothing_into_the_next_file(tmp_path):
    path = tmp_path / "trace.json"
    trace = Perfetto(path, sample_s=10.0, close_s=0.2)
    m = build(PROGRAM, "main", plugins=[trace])
    run_jobs(m, jobs(1))
    old = trace.worker
    m.stop()

    # Loaded again (a new machine), then the first load's writer runs a late task
    m2 = build(PROGRAM, "main", plugins=[trace])
    try:
        time.sleep(0.3)
        before = path.read_text(encoding="utf-8")
        old.handle(("raw", {"ph": "M", "name": "late"}))
        assert path.read_text(encoding="utf-8") == before
    finally:
        m2.stop()
