"""The balancer in front of real services over HTTP; each service's machine runs a job in a fixed
time on a fixed number of slots, so its rate is known."""

import json
import queue
import threading
import time
import urllib.request

import pytest

from togeari.core.contracts.io import JobRecord
from togeari.serve import Balancer, Service


class TimedMachine:
    """Reads the source into ``slots`` workers (with ``eager``, as fast as it comes); each job
    takes ``seconds``; no job ends before ``warmup`` seconds into the run."""

    def __init__(self, seconds: float, slots: int, eager: bool = False, warmup: float = 0.0):
        self.seconds, self.slots, self.eager, self.warmup = seconds, slots, eager, warmup
        self.ran: list[str] = []
        self.halt = threading.Event()

    def command(self, *a, **kw):
        raise AssertionError("no plugins here")

    def run(self, source, sink):
        work: queue.Queue = queue.Queue(maxsize=0 if self.eager else self.slots)
        ready = time.monotonic() + self.warmup

        def worker():
            time.sleep(max(0.0, ready - time.monotonic()))
            while (item := work.get()) is not None:
                time.sleep(self.seconds)
                if self.halt.is_set():
                    continue
                self.ran.append(item.key)
                sink.write(JobRecord(item.key, "main", True, {"x": item.inputs["x"]}))

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.slots)]
        for t in threads:
            t.start()
        for item in source:
            work.put(item)
        for _ in threads:
            work.put(None)
        for t in threads:
            t.join()

    def stop(self, drain=True):
        return None


def post(url: str, jobs: list[dict]) -> list[dict]:
    body = "".join(json.dumps(j) + "\n" for j in jobs).encode()
    req = urllib.request.Request(
        f"{url}/jobs", body, {"Content-Type": "application/x-ndjson"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return [json.loads(line) for line in r if line.strip()]


def status(url: str) -> dict:
    with urllib.request.urlopen(f"{url}/status", timeout=5) as r:
        return json.loads(r.read())


@pytest.fixture
def cluster():
    made: list = []

    def make(speeds, eager=False, warmup=0.0, **kw):
        knobs = {"lead_s": 0.5, "min_queue": 2, "batch": 4, "dead_s": 0.5, **kw}
        balancer = Balancer(port=0, **knobs)
        burl = balancer.start()
        made.append(balancer)
        services = []
        for i, (seconds, slots) in enumerate(speeds):
            machine = TimedMachine(seconds, slots, eager, warmup)
            service = Service(machine, port=0, balancer=burl, name=f"s{i}", report_s=0.1)
            service.start()
            made.append(service)
            services.append((service, machine))
        deadline = time.monotonic() + 5
        while len(status(burl)["servers"]) < len(services):
            assert time.monotonic() < deadline, "the services never reported"
            time.sleep(0.05)
        return burl, balancer, services

    yield make
    for thing in reversed(made):
        thing.stop(drain=False)


def test_every_job_answered_once_with_its_server(cluster):
    burl, _, services = cluster([(0.01, 2), (0.01, 2)])
    answers = post(burl, [{"key": f"k{i}", "inputs": {"x": i}} for i in range(60)])
    assert sorted(a["key"] for a in answers) == sorted(f"k{i}" for i in range(60))
    assert all(a["ok"] and a["outputs"]["x"] == int(a["key"][1:]) for a in answers)
    assert {a["server"] for a in answers} == {"s0", "s1"}
    # Every job of a server on its one link
    assert {n: s["links"] for n, s in status(burl)["servers"].items()} == {"s0": 1, "s1": 1}
    ran = [k for _, m in services for k in m.ran]
    assert sorted(ran) == sorted(f"k{i}" for i in range(60))


def ends(burl: str, n: int) -> dict[str, float]:
    """Post ``n`` jobs; when each server's last answer came."""
    jobs = [{"key": f"k{i}", "inputs": {"x": i}} for i in range(n)]
    t0 = time.monotonic()
    done_at: dict[str, float] = {}
    body = "".join(json.dumps(j) + "\n" for j in jobs).encode()
    req = urllib.request.Request(
        f"{burl}/jobs", body, {"Content-Type": "application/x-ndjson"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        for line in r:
            if line.strip():
                done_at[json.loads(line)["server"]] = time.monotonic() - t0
    return done_at


def test_the_faster_server_takes_more_and_both_finish_together(cluster):
    # s0 runs 4x the rate of s1: 4 slots at 20 ms against 1 slot at 20 ms
    burl, _, services = cluster([(0.02, 4), (0.02, 1)])
    done_at = ends(burl, 300)
    fast, slow = (len(m.ran) for _, m in services)
    assert fast + slow == 300
    assert 2.5 < fast / slow < 6, (fast, slow)
    # Ideal wall 300 / 250 per s = 1.2 s; the two ends within a few of the slow server's jobs
    assert abs(done_at["s0"] - done_at["s1"]) < 0.4, done_at


def test_hold_bounds_the_ends_of_machines_that_read_everything(cluster):
    # Machines that admit every job they are sent and answer nothing for their first second (no
    # rate known): only ``hold_s`` keeps jobs at the balancer until the rates are known
    speeds = [(0.02, 4), (0.02, 1)]
    burl, _, services = cluster(speeds, eager=True, warmup=1.0, hold_s=0.3, min_queue=8)
    done_at = ends(burl, 300)
    fast, slow = (len(m.ran) for _, m in services)
    assert 2.5 < fast / slow < 6, (fast, slow)
    assert abs(done_at["s0"] - done_at["s1"]) < 0.5, done_at


def test_a_server_that_hangs_has_its_jobs_run_elsewhere(cluster):
    burl, _, services = cluster([(0.02, 2), (0.02, 2)])
    (dying, machine), (_, other) = services

    def hang():
        # It stops running jobs and stops reporting; its open batches never end
        machine.halt.set()
        dying.reporting.clear()
        dying.poke.set()

    killer = threading.Timer(0.3, hang)
    killer.start()
    answers = post(burl, [{"key": f"k{i}", "inputs": {"x": i}} for i in range(120)])
    killer.join()
    assert sorted(a["key"] for a in answers) == sorted(f"k{i}" for i in range(120))
    assert all(a["ok"] for a in answers), [a for a in answers if not a["ok"]][:3]
    assert len(set(machine.ran) | set(other.ran)) == 120
    assert {a["server"] for a in answers if a["key"] not in machine.ran} == {"s1"}


def test_refused_jobs_go_back_to_the_queue(cluster):
    burl, _, services = cluster([(0.01, 2)])
    service = services[0][0]
    # A key already in the service is refused there until it leaves; then it runs
    service.waiting["k0"] = None
    threading.Timer(0.2, lambda: service.waiting.pop("k0", None)).start()
    answers = post(burl, [{"key": f"k{i}", "inputs": {"x": i}} for i in range(20)])
    assert sorted(a["key"] for a in answers) == sorted(f"k{i}" for i in range(20))
    assert all(a["ok"] for a in answers), [a for a in answers if not a["ok"]]
    assert status(burl)["requeued"] >= 1


def test_duplicate_keys_are_refused_at_the_balancer(cluster):
    burl, _, _ = cluster([(0.2, 1)])
    answers = post(burl, [{"key": "a", "inputs": {"x": 1}}, {"key": "a", "inputs": {"x": 2}}])
    by_ok = {a["ok"]: a for a in answers}
    assert by_ok[True]["outputs"]["x"] == 1
    assert "already in the balancer" in by_ok[False]["error"]
