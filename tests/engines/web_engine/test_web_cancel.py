"""WebCallEngine cancellation: early cancels never leave the connection pools stuck."""

import random
import threading
import time

from echo_server import Server

from togeari.adapters.http import JsonHttp
from togeari.core.contracts.results import Cancelled, Ok
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.web import WebCallEngine


def call(x):
    return WorkItem("svc", {"x": x}, JsonHttp("/double", parse=lambda b: b["y"]), protocol="http")


class Answers:
    """Results per ticket, collected from ``done`` on any thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_ticket: dict[int, list] = {}

    def done(self, ticket, results):
        with self.lock:
            self.by_ticket.setdefault(ticket.id, []).append(results[0])

    def wait(self, n: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.by_ticket) >= n:
                    return True
            time.sleep(0.01)
        return False


def one_round(rng: random.Random) -> None:
    server = Server()
    engine = WebCallEngine(server.url, max_inflight=8, pool_size=2)
    engine.start()
    try:
        # Requests cancelled within their first milliseconds: each answers exactly once
        early = Answers()
        for i in range(64):
            t = engine.submit(call(i), early.done)
            time.sleep(rng.random() * 0.003)
            engine.cancel(t)
        assert early.wait(64)
        assert all(len(rs) == 1 for rs in early.by_ticket.values())
        assert all(isinstance(rs[0], Ok | Cancelled) for rs in early.by_ticket.values())

        # Plain requests afterwards all answer Ok
        plain = Answers()
        for i in range(32):
            deadline = time.monotonic() + 10.0
            while engine.capacity() <= 0 and time.monotonic() < deadline:
                time.sleep(0.001)
            engine.submit(call(i), plain.done)
        assert plain.wait(32), f"{len(plain.by_ticket)} of 32 answered"
        assert all(isinstance(rs[0], Ok) for rs in plain.by_ticket.values())
    finally:
        engine.stop()
        server.close()


def test_plain_requests_answer_after_a_burst_of_early_cancels():
    rng = random.Random(0)
    for _ in range(5):
        one_round(rng)
