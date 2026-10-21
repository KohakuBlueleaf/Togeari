"""WebCallEngine lifecycle: wakes after a dropped answer, health after a restart, Retry-After
bounds, one I/O loop per start."""

import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.adapters.http import JsonHttp
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.common.state import EngineState
from togeari.engines.python import PythonCallEngine
from togeari.engines.web import WebCallEngine
from togeari.io import Iterable, Memory

SLOW_S = 1.0


class Handler(BaseHTTPRequestHandler):
    """``/slow`` answers after SLOW_S; ``/down`` answers 503 with ``Retry-After``; ``/health``
    counts its calls."""

    protocol_version = "HTTP/1.1"
    health_calls = 0
    retry_after = "0"

    def log_message(self, *args):
        pass

    def reply(self, code, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        type(self).health_calls += 1
        self.reply(200, {"ok": True})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(size) or b"null")
        match self.path:
            case "/slow":
                time.sleep(SLOW_S)
                self.reply(200, {"y": 2 * body["x"]})
            case _:
                self.reply(503, {"error": "down"}, [("Retry-After", type(self).retry_after)])


@pytest.fixture
def server():
    handler = type("H", (Handler,), {"health_calls": 0, "retry_after": "0"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", handler
    httpd.shutdown()
    httpd.server_close()


def until(cond, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


def run_one(engine, work, timeout=10.0):
    got, event = [], threading.Event()
    engine.submit(work, lambda ticket, results: (got.extend(results), event.set()))
    assert event.wait(timeout), "no answer"
    return got[0]


lib = NodeLibrary("wake")


@lib.node(outputs=["y"])
def slow(x: int) -> int:
    return 2 * x


@lib.node(outputs=["z"])
def boom(x: int) -> int:
    time.sleep(0.2)
    raise ValueError("boom")


# The web call after boom is squashed (its request cancelled) and the handler calls again
PROGRAM = """
@def (x: int)main(y):
    @try:
        (x)boom(z)
        (x)slow(y)
    @except err:
        (x)slow(y)
"""


def test_a_full_web_unit_takes_queued_work_once_a_cancelled_request_ends(server):
    url, _ = server
    web = Lane([WebCallEngine(url, max_inflight=1, pool_size=1, name="svc")], name="web")
    py = Lane([PythonCallEngine(workers=1, mode="thread", name="py")], name="py")
    machine = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[lib],
        bind={
            "slow": Use(web, adapter=JsonHttp("/slow", parse=lambda b: b["y"])),
            "boom": Use(py),
            "kir.std.*": Inline(),
        },
    )
    sink, finished = Memory(), threading.Event()

    def go():
        with machine:
            machine.run(Iterable([{"x": 3}]), sink)
        finished.set()

    threading.Thread(target=go, daemon=True).start()
    assert finished.wait(SLOW_S * 2 + 8.0), "the lane never took the handler's call"
    assert [r.outputs for r in sink.records] == [{"y": 6}]


def test_the_health_probe_runs_after_a_restart(server):
    url, handler = server
    engine = WebCallEngine(url, max_inflight=2, health_path="/health")
    engine.start()
    try:
        # A probe left pending by stop is cleared by the next start
        engine.health_pending = True
        engine.stop()
        engine.start()
        run_one(engine, WorkItem("svc", {"x": 1}, JsonHttp("/down"), protocol="http"))
        before = handler.health_calls
        assert until(lambda: engine.ready())
        assert handler.health_calls > before
    finally:
        engine.stop()


@pytest.mark.parametrize("header", ["inf", "1e12"])
def test_a_huge_retry_after_blocks_at_most_max_retry_after_s(server, header):
    url, handler = server
    handler.retry_after = header
    engine = WebCallEngine(url, max_inflight=2, max_retry_after_s=0.5)
    engine.start()
    try:
        run_one(engine, WorkItem("svc", {"x": 1}, JsonHttp("/down"), protocol="http"))
        assert not engine.ready()
        assert until(engine.ready, timeout=3.0)
    finally:
        engine.stop()


def test_a_healthy_unit_is_never_probed_and_an_unhealthy_one_is(server):
    url, handler = server
    engine = WebCallEngine(url, max_inflight=2, health_path="/health")
    engine.start()
    try:
        # Healthy: a second of ready() polls sends no health GET
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert engine.ready()
            time.sleep(0.01)
        assert handler.health_calls == 0

        # After a 503 the unit probes until a GET succeeds
        run_one(engine, WorkItem("svc", {"x": 1}, JsonHttp("/down"), protocol="http"))
        assert until(engine.ready)
        assert handler.health_calls >= 1
    finally:
        engine.stop()


def test_a_stop_during_start_leaves_the_engine_stopped_and_startable(server):
    url, _ = server
    engine = WebCallEngine(url, max_inflight=2)

    # A stop while the engine is still starting (no loop yet)
    engine.state = EngineState.STARTING
    engine.stop()
    assert engine.state == EngineState.STOPPED
    engine.start()
    try:
        assert until(engine.ready)
    finally:
        engine.stop()


def test_a_request_waiting_for_a_rate_token_holds_no_connection(server):
    url, _ = server
    engine = WebCallEngine(url, max_inflight=4, rate=(1.0, 1.0))
    engine.start()
    try:
        for i in range(3):
            engine.submit(call_slow(i), lambda ticket, results: None)

        # One request has its token and runs; the two others wait without a connection
        time.sleep(0.3)
        assert sum(p.inflight for p in engine.pools) == 1
    finally:
        engine.stop()


def call_slow(x):
    return WorkItem("svc", {"x": x}, JsonHttp("/slow", parse=lambda b: b["y"]), protocol="http")


def test_concurrent_starts_open_one_io_loop(server):
    url, _ = server
    engine = WebCallEngine(url, max_inflight=2, name="once")
    threads = [threading.Thread(target=engine.start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert until(engine.ready)
        loops = [t for t in threading.enumerate() if t.name == "once-io"]
        assert len(loops) == 1
    finally:
        engine.stop()


def test_a_start_builds_one_tls_context_for_every_client(server, monkeypatch):
    # Each context loads the CA store; one per client made a 64-request engine's start slow
    url, _ = server
    built = []
    original = ssl.create_default_context

    def counting(*args, **kwargs):
        built.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)
    engine = WebCallEngine(url, max_inflight=64, pool_size=2, health_path="/health")
    engine.start()
    try:
        assert len(engine.pools) == 32 and engine.health_client is not None
        assert len(built) == 1, len(built)
    finally:
        engine.stop()
