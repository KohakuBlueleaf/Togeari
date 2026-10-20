"""WebCallEngine against a local HTTP service: behaviour checks, error classes, rate, probe."""

import threading
import time

import pytest
from echo_server import Server

from togeari.adapters.http import JsonHttp
from togeari.core.contracts.results import ItemError, Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.web import WebCallEngine
from togeari.engines.web.engine import pool_sizes
from togeari.testing import check_engine


def y_of(body):
    return body["y"]


def call(path, x):
    return WorkItem("svc", {"x": x}, JsonHttp(path, parse=y_of), protocol="http")


def until(cond, timeout=10.0):
    """Wait for ``cond()``; its last value."""
    deadline = time.monotonic() + timeout
    while not (value := cond()) and time.monotonic() < deadline:
        time.sleep(0.005)
    return value


def run_one(engine, work, timeout=30.0):
    got = []
    event = threading.Event()

    def done(ticket, results):
        got.extend(results)
        event.set()

    engine.submit(work, done)
    assert event.wait(timeout), "no answer"
    return got[0]


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


@pytest.mark.parametrize("mode", ["block", "cancel"])
def test_web_engine_keeps_every_rule(server, mode):
    report = check_engine(
        lambda: WebCallEngine(server.url, max_inflight=8, on_unit_error=mode),
        [call("/double", i) for i in range(8)],
        failing_items=[call("/fail", 1)],
        kill=server.close,
        rounds=150,
    )
    assert report.ok, str(report)


def test_error_classes(server):
    engine = WebCallEngine(server.url, max_inflight=4)
    engine.start()
    try:
        assert run_one(engine, call("/double", 4)) == Ok(8, True)

        # A server error and a missing route belong to the item.
        assert isinstance(run_one(engine, call("/fail", 1)), ItemError)
        assert isinstance(run_one(engine, call("/nope", 1)), ItemError)
        assert engine.ready()

        # 503 blocks the unit; 429 blocks it until Retry-After has passed.
        assert isinstance(run_one(engine, call("/down", 1)), UnitError)
        busy = run_one(engine, call("/busy", 1))
        assert isinstance(busy, UnitError) and busy.retry_after == 1.0
        assert not engine.ready()
        wake = engine.wake_at()
        assert until(engine.ready)
        assert time.monotonic() >= wake
        assert engine.load().extra["unit_errors"] == 2
    finally:
        engine.stop()


def test_rate_limit_paces_requests(server):
    engine = WebCallEngine(server.url, max_inflight=16, rate=(20.0, 1))
    engine.start()
    try:
        done = threading.Semaphore(0)
        t0 = time.perf_counter()
        for i in range(6):
            engine.submit(call("/double", i), lambda t, r: done.release())
        assert engine.capacity() == 0
        for _ in range(6):
            assert done.acquire(timeout=10.0)
        elapsed = time.perf_counter() - t0
    finally:
        engine.stop()
    # Burst 1 then 20 per second: five more tokens take about 0.25 s.
    assert elapsed >= 0.23, elapsed


def test_spec_is_identity_only(server):
    engine = WebCallEngine(server.url, max_inflight=4, rate=(100.0, 2))
    before = engine.spec()
    engine.start()
    try:
        assert isinstance(run_one(engine, call("/slow", 1)), Ok)
        after = engine.spec()
    finally:
        engine.stop()
    assert after == before


def test_wake_at_is_when_the_bucket_allows_the_next_request(server):
    engine = WebCallEngine(server.url, max_inflight=16, rate=(2.0, 1))
    engine.start()
    try:
        assert engine.wake_at() is None
        done = threading.Event()
        before = time.monotonic()
        engine.submit(call("/double", 1), lambda t, r: done.set())

        # The token is taken (now or by the I/O loop later): the next one is 0.5 s after that.
        assert engine.capacity() == 0
        wake = engine.wake_at()
        after = time.monotonic()
        assert wake is not None and before + 0.5 - 1e-6 <= wake <= after + 0.5 + 1e-6
        assert until(lambda: engine.capacity() > 0)
        assert time.monotonic() >= wake
        assert engine.wake_at() is None
        assert done.wait(10.0)
    finally:
        engine.stop()


def test_wake_at_is_the_end_of_a_retry_after_block(server):
    engine = WebCallEngine(server.url, max_inflight=4)
    engine.start()
    try:
        before = time.monotonic()
        assert isinstance(run_one(engine, call("/busy", 1)), UnitError)
        after = time.monotonic()
        wake = engine.wake_at()

        # Retry-After is 1 s from the answer, which came between before and after
        assert wake is not None and before + 1.0 <= wake <= after + 1.0
        assert not engine.ready()
    finally:
        engine.stop()


def test_a_restart_starts_with_no_request_in_flight(server):
    engine = WebCallEngine(server.url, max_inflight=4, rate=(100.0, 4))
    engine.start()
    engine.stop()

    # State a stop can leave behind when its loop ends before draining its calls
    engine.inflight = 3
    engine.unpaced.add(-1)
    engine.inbox.append((engine._launch, None))
    engine.kicked = True

    engine.start()
    try:
        assert engine.capacity() == 4 and engine.load().running == 0
        assert run_one(engine, call("/double", 2), timeout=10.0) == Ok(4, True)
        assert engine.capacity() == 4
    finally:
        engine.stop()


def test_requests_spread_over_client_pools(server):
    assert pool_sizes(20, 8) == [7, 7, 6]
    assert pool_sizes(8, 8) == [8] and pool_sizes(3, 8) == [3]
    engine = WebCallEngine(server.url, max_inflight=20, pool_size=8)
    engine.start()
    try:
        left = threading.Semaphore(0)
        got = []
        for i in range(20):
            engine.submit(call("/slow", i), lambda t, r: (got.extend(r), left.release()))
        assert engine.capacity() == 0
        for _ in range(20):
            assert left.acquire(timeout=10.0)
        assert [p.size for p in engine.pools] == [7, 7, 6]
    finally:
        engine.stop()
    assert sorted(r.value for r in got) == [2 * i for i in range(20)]


def test_load_reports_probe_and_latency(server):
    engine = WebCallEngine(server.url, load_probe=lambda: {"queue": 3}, probe_interval_s=0.05)
    engine.start()
    try:
        assert isinstance(run_one(engine, call("/slow", 2)), Ok)
        until(lambda: engine.load().extra["probe"] == {"queue": 3})
        extra = engine.load().extra
    finally:
        engine.stop()
    assert extra["probe"] == {"queue": 3}
    assert extra["latency_ms"] >= 50.0


def test_health_path_gates_ready_after_unit_error(server):
    engine = WebCallEngine(server.url, health_path="/health")
    engine.start()
    try:
        assert isinstance(run_one(engine, call("/down", 1)), UnitError)
        assert not engine.ready()
        assert until(engine.ready)
    finally:
        engine.stop()
