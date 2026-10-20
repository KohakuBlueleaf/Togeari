"""WebCallEngine unit errors: the unit blocks; ``on_unit_error`` sets what the other requests
in flight get ("block": they finish normally; "cancel": the same error, at once)."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from togeari.adapters.http import JsonHttp
from togeari.core.contracts.results import Ok, UnitError
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.web import WebCallEngine

HOLD_S = 10.0


def until(cond, timeout=10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


class HoldServer(ThreadingHTTPServer):
    """A listen backlog for every request the tests hold at once."""

    daemon_threads = True
    request_queue_size = 64


class HoldHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    release: threading.Event
    arrived: threading.Semaphore

    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(size) or b"null")
        match self.path:
            case "/hold":
                # Held until released, then answered normally
                self.arrived.release()
                self.release.wait(HOLD_S)
                self.reply(200, {"y": 2 * body["x"]})
            case "/down":
                self.reply(503, {"error": "down"})
            case "/busy":
                # A 503 that blocks the unit for one second
                data = json.dumps({"error": "busy"}).encode()
                self.send_response(503)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            case _:
                self.reply(404, {"error": self.path})


@pytest.fixture
def held_server():
    release = threading.Event()
    arrived = threading.Semaphore(0)
    handler = type("Handler", (HoldHandler,), {"release": release, "arrived": arrived})
    httpd = HoldServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", release, arrived
    release.set()
    httpd.shutdown()
    httpd.server_close()


def y_of(body):
    return body["y"]


def call(path, x):
    return WorkItem("svc", {"x": x}, JsonHttp(path, parse=y_of), protocol="http")


def test_cancel_mode_answers_every_request_in_flight_at_once(held_server):
    url, release, arrived = held_server
    engine = WebCallEngine(url, max_inflight=16, on_unit_error="cancel")
    engine.start()
    got: dict[int, tuple[float, object]] = {}
    lock = threading.Lock()
    all_in = threading.Event()

    def done_for(i):
        def done(ticket, results):
            with lock:
                got[i] = (time.monotonic(), results[0])
                if len(got) == 8:
                    all_in.set()

        return done

    try:
        # Seven requests held by the service, then one that answers 503
        for i in range(7):
            engine.submit(call("/hold", i), done_for(i))
        for _ in range(7):
            assert arrived.acquire(timeout=5.0)
        t0 = time.monotonic()
        engine.submit(call("/down", 7), done_for(7))

        # Every item is answered with the unit error well before the held ones would return
        assert all_in.wait(3.0), f"answered {sorted(got)}"
        assert all(t - t0 < 3.0 for t, _ in got.values())
        assert all(isinstance(r, UnitError) for _, r in got.values())
        assert {str(r.error) for _, r in got.values()} == {str(got[7][1].error)}

        # The held requests still run and hold their slots; the counters show the spread
        load = engine.load()
        assert load.running == 7
        assert load.extra["unit_errors"] == 1
        assert load.extra["unit_error_items"] == 7

        # Released, they end, free their slots, and answer nothing a second time
        release.set()
        assert until(lambda: engine.load().running == 0)
        time.sleep(0.2)
        assert len(got) == 8
        assert all(isinstance(r, UnitError) for _, r in got.values())
    finally:
        release.set()
        engine.stop()


def test_block_mode_lets_running_requests_finish_and_takes_no_new_work(held_server):
    url, release, arrived = held_server
    engine = WebCallEngine(url, max_inflight=16)
    engine.start()
    got: dict[int, object] = {}
    answered = threading.Semaphore(0)

    def done_for(i):
        def done(ticket, results):
            got[i] = results[0]
            answered.release()

        return done

    try:
        # Seven requests held by the service, then one 503 with Retry-After 1
        for i in range(7):
            engine.submit(call("/hold", i), done_for(i))
        for _ in range(7):
            assert arrived.acquire(timeout=5.0)
        engine.submit(call("/busy", 7), done_for(7))
        assert answered.acquire(timeout=5.0)

        # Only the request that saw the 503 is answered; the unit takes no new work
        assert list(got) == [7] and isinstance(got[7], UnitError)
        assert engine.capacity() == 0 and not engine.ready()
        assert engine.load().running == 7
        assert engine.load().extra["unit_error_items"] == 0

        # The held requests finish normally once the service answers them
        release.set()
        for _ in range(7):
            assert answered.acquire(timeout=5.0)
        assert [got[i] for i in range(7)] == [Ok(2 * i, True) for i in range(7)]

        # The block ends after Retry-After
        deadline = time.monotonic() + 3.0
        while engine.capacity() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert engine.capacity() == 16 and engine.ready()
    finally:
        engine.stop()


def test_an_unknown_unit_error_mode_is_refused():
    with pytest.raises(ValueError, match="on_unit_error"):
        WebCallEngine("http://127.0.0.1:1", on_unit_error="drop")


def test_an_item_error_leaves_the_other_requests_running(held_server):
    url, release, arrived = held_server
    engine = WebCallEngine(url, max_inflight=4)
    engine.start()
    got = {}
    answered = threading.Semaphore(0)

    def done_for(i):
        def done(ticket, results):
            got[i] = results[0]
            answered.release()

        return done

    try:
        engine.submit(call("/hold", 1), done_for(1))
        assert arrived.acquire(timeout=5.0)
        engine.submit(call("/nope", 2), done_for(2))
        assert answered.acquire(timeout=5.0)
        assert list(got) == [2]

        # The held request is still in flight and answers normally once released
        release.set()
        assert answered.acquire(timeout=5.0)
        assert got[1] == Ok(2, True)
    finally:
        engine.stop()
