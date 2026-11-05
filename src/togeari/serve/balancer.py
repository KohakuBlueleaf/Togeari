"""A load balancer in front of Togeari services: clients post jobs to it as to one service, and
it hands each job to the service that will run out of work first.

Routes (a service's, ``togeari.serve.service``):

- ``POST /jobs``: as a service's (NDJSON jobs in, one answer line per job back as each finishes,
  keys unique among the jobs here); each answer line also carries ``server``: the service that
  ran the job.
- ``GET /status``: jobs pending (held here), at a server, answered, failed, refused, requeued;
  every server's state.
- ``POST /servers/report``: a service's report (``Service(balancer=...)``): ``{"name", "url",
  "leaving", "status"}``.
- ``GET /``: the routes.

Servers: those that report, and ``servers`` (URLs) given at construction, whose ``GET /status``
the balancer reads every ``poll_s`` instead. Servers behind a balancer take their jobs from it
alone.

Links: one streaming ``POST /jobs`` per server, open while the server is up: jobs go down it as
chunks of NDJSON, answers come back up it. However many jobs a server holds, it costs one
connection.

Dispatch: a server *holds* the jobs sent to it and not yet answered (``held``, counted here); of
those, ``resident`` (from its last report) are inside its machine, the rest (``backlog``) wait
in its queue or on the wire. A server takes jobs while

- its backlog is under ``max(min_queue, rate * lead_s)``: about ``lead_s`` seconds of its own
  work wait at its door, so it never runs dry between two reports; and
- with ``hold_s``, all it holds is under ``max(min_queue, rate * hold_s)``: a machine reads its
  queue as far as its admission lets it, so without this cap every job goes to the first
  servers to start. With it, every server holds about ``hold_s`` seconds of its own work, and
  the servers' last jobs end about together. ``hold_s`` covers a job's time through the
  machine (less starves its pipeline); ``min_queue`` is what a server holds before its rate is
  known.

``rate`` is the server's answers per second (its own count over a sliding window). Among the
servers that take jobs, the one with the least backlog against its mark takes the next batch
(at most ``batch`` jobs).

Failure: a server that has not reported for ``dead_s`` (or whose status cannot be read) takes
nothing new, and its link is closed; the jobs it held go back to the front of the queue. So do
the jobs of a link that breaks, and a job a server refuses (``"refused": true``: it was
stopping, say); that server is then sent nothing for ``REFUSED_PAUSE_S``. A job is sent at most
``tries`` times in all.
"""

import contextlib
import http.client
import itertools
import json
import logging
import socket
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from togeari.serve.service import _END, NDJSON, _Handler, _Request, token_ok

log = logging.getLogger("togeari.serve")

# Seconds a server that refused a job or whose link broke is sent nothing (at most ``dead_s``)
REFUSED_PAUSE_S = 1.0


@dataclass
class Server:
    """One service as the balancer sees it."""

    name: str
    url: str
    polled: bool
    # The last status: running, error, load (received, queued, resident, rate)
    status: dict = field(default_factory=dict)
    seen: float = 0.0
    leaving: bool = False
    down_until: float = 0.0
    held: set = field(default_factory=set)
    link: "_Link | None" = None
    links: int = 0
    sent: int = 0
    answered: int = 0
    failed: int = 0


@dataclass
class _Job:
    key: str
    line: dict
    req: _Request
    tries: int = 0
    # The server that holds it
    at: Server | None = None


class _Link:
    """One streaming ``POST /jobs`` to a server: ``put`` jobs down it, answers come back to the
    balancer; when it breaks, the balancer is told once."""

    def __init__(self, balancer: "Balancer", server: Server):
        self.balancer, self.server = balancer, server
        self.cv = threading.Condition()
        self.out: deque[bytes] = deque()
        self.closing = False
        self.conn: http.client.HTTPConnection | None = None
        self.writer = threading.Thread(
            target=self._write, name=f"togeari-balance-to-{server.name}", daemon=True
        )
        self.writer.start()

    def put(self, lines: list[dict]) -> None:
        data = "".join(json.dumps(line) + "\n" for line in lines).encode()
        with self.cv:
            self.out.append(data)
            self.cv.notify()

    def close(self) -> None:
        """End the body (the server answers what it holds, then ends the answer)."""
        with self.cv:
            self.closing = True
            self.cv.notify()

    def kill(self) -> None:
        """Drop the connection now."""
        self.close()
        conn = self.conn
        if conn is not None and conn.sock is not None:
            with contextlib.suppress(OSError):
                conn.sock.shutdown(socket.SHUT_RDWR)

    def _write(self) -> None:
        url = urlparse(self.server.url)
        try:
            self.conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=None)
            self.conn.putrequest("POST", f"{url.path}/jobs")
            self.conn.putheader("Content-Type", NDJSON)
            self.conn.putheader("Transfer-Encoding", "chunked")
            for k, v in self.balancer._auth().items():
                self.conn.putheader(k, v)
            self.conn.endheaders()
            threading.Thread(
                target=self._read, name=f"togeari-balance-from-{self.server.name}", daemon=True
            ).start()
            while True:
                with self.cv:
                    while not self.out and not self.closing:
                        self.cv.wait()
                    data = b"".join(self.out)
                    self.out.clear()
                    closing = self.closing
                if data:
                    self.conn.send(b"%x\r\n%s\r\n" % (len(data), data))
                if closing:
                    self.conn.send(b"0\r\n\r\n")
                    return
        except (OSError, http.client.HTTPException) as exc:
            self.balancer._link_down(self, exc)

    def _read(self) -> None:
        exc: BaseException | None = None
        try:
            resp = self.conn.getresponse()
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            for raw in resp:
                if raw.strip():
                    self.balancer._answer(self.server, json.loads(raw))
        except (OSError, ValueError, http.client.HTTPException) as exc_:
            exc = exc_
        self.balancer._link_down(self, exc or EOFError("the answer ended"))
        with contextlib.suppress(OSError):
            self.conn.close()


class Balancer:
    """Jobs in on ``host:port``, out to the servers (see the module)."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8780,
        token: str | None = None,
        servers: tuple | list = (),
        lead_s: float = 2.0,
        min_queue: int = 32,
        hold_s: float | None = 30.0,
        batch: int = 64,
        dead_s: float = 5.0,
        poll_s: float = 0.5,
        tries: int = 3,
        max_pending: int = 100_000,
    ):
        self.host, self.port, self.token = host, port, token
        self.lead_s, self.min_queue, self.hold_s = lead_s, min_queue, hold_s
        self.batch, self.dead_s, self.poll_s, self.tries = batch, dead_s, poll_s, tries
        self.max_pending = max_pending
        self.cv = threading.Condition()
        self.pending: deque[str] = deque()
        self.jobs: dict[str, _Job] = {}
        self.servers: dict[str, Server] = {}
        for url in servers:
            url = url.rstrip("/")
            self.servers[url] = Server(url, url, polled=True)
        self.keys = itertools.count(1)
        self.counts = {"answered": 0, "failed": 0, "refused": 0, "requeued": 0}
        self.started = 0.0
        self.stopping = False
        self.server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> str:
        """Start the HTTP server, the dispatcher and the poller; the bound URL."""
        self.started = time.time()
        handler = type("BalancerHandler", (_BalancerHandler,), {"service": self})
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        for target, name in (
            (lambda: self.server.serve_forever(poll_interval=0.2), "togeari-balance-http"),
            (self._dispatch, "togeari-balance-dispatch"),
            (self._poll, "togeari-balance-poll"),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()
        return f"http://{self.host}:{self.port}"

    def stop(self, drain: bool = True, timeout: float = 600.0) -> None:
        """Stop taking jobs; with ``drain``, wait (up to ``timeout``) for the jobs held."""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        deadline = time.monotonic() + timeout
        with self.cv:
            while drain and self.jobs and time.monotonic() < deadline:
                self.cv.wait(0.5)
            self.stopping = True
            left, self.jobs = self.jobs, {}
            self.pending.clear()
            links = [s.link for s in self.servers.values() if s.link is not None]
            for s in self.servers.values():
                s.link = None
                s.held.clear()
            self.cv.notify_all()
        for link in links:
            link.kill()
        for job in left.values():
            job.req.lines.put({"key": job.key, "ok": False, "error": "the balancer stopped"})
            self._settle(job.req)

    # ------------------------------------------------------------ jobs in
    def submit(self, job: Any, req: _Request) -> None:
        """One posted job, held until a server takes it; while ``max_pending`` jobs wait here,
        the poster is held back."""
        key = job.get("key") if isinstance(job, dict) else None
        try:
            if not isinstance(job, dict):
                raise ValueError("a job is a JSON object")
            with self.cv:
                while len(self.pending) >= self.max_pending and not self.stopping:
                    self.cv.wait(1.0)
                if self.stopping:
                    raise RuntimeError("the balancer is stopping")
                key = str(key) if key is not None else f"job-{next(self.keys)}"
                if key in self.jobs:
                    raise ValueError(f"key {key!r} is already in the balancer")
                self.jobs[key] = _Job(key, {**job, "key": key}, req)
                self.pending.append(key)
                with req.lock:
                    req.owed += 1
                self.cv.notify_all()
        except Exception as exc:  # noqa: BLE001 - a refused job is answered, the stream goes on
            with self.cv:
                self.counts["refused"] += 1
            req.lines.put({"key": key, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def finish_reading(self, req: _Request) -> None:
        with req.lock:
            req.read_all = True
            if req.owed == 0:
                req.lines.put(_END)

    def _settle(self, req: _Request) -> None:
        with req.lock:
            req.owed -= 1
            if req.read_all and req.owed == 0:
                req.lines.put(_END)

    # ------------------------------------------------------------ servers
    def report(self, body: dict) -> None:
        """A server's report (``POST /servers/report``)."""
        name = str(body.get("name") or body.get("url"))
        with self.cv:
            server = self.servers.get(name)
            if server is None:
                server = self.servers[name] = Server(name, str(body["url"]).rstrip("/"), False)
            server.url = str(body.get("url") or server.url).rstrip("/")
            server.status = body.get("status") or {}
            server.seen = time.monotonic()
            server.leaving = bool(body.get("leaving"))
            self.cv.notify_all()

    def _poll(self) -> None:
        """The status of every server given by URL that does not report itself."""
        while not self.stopping:
            for server in list(self.servers.values()):
                if not server.polled:
                    continue
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(f"{server.url}/status", headers=self._auth()),
                        timeout=self.dead_s,
                    ) as r:
                        status = json.loads(r.read())
                    with self.cv:
                        server.status, server.seen = status, time.monotonic()
                        self.cv.notify_all()
                except (OSError, ValueError):
                    with self.cv:
                        server.down_until = time.monotonic() + self.dead_s
            time.sleep(self.poll_s)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # ------------------------------------------------------------ dispatch
    def marks(self, server: Server) -> tuple[int, float, float | None]:
        """``(backlog, backlog mark, held mark)`` of a server (``cv`` held)."""
        load = server.status.get("load") or {}
        rate = float(load.get("rate") or 0.0)
        backlog = max(0, len(server.held) - int(load.get("resident") or 0))
        low = max(float(self.min_queue), rate * self.lead_s)
        cap = None if self.hold_s is None else max(float(self.min_queue), rate * self.hold_s)
        return backlog, low, cap

    def up(self, server: Server, now: float) -> bool:
        return (
            not server.leaving
            and bool(server.status.get("running"))
            and now - server.seen <= self.dead_s
            and now >= server.down_until
        )

    def _room(self, server: Server, now: float) -> tuple[float, int] | None:
        """``(score, jobs it takes now)`` of a server, or None (``cv`` held)."""
        if not self.up(server, now):
            return None
        backlog, low, cap = self.marks(server)
        n = low - backlog
        if cap is not None:
            n = min(n, cap - len(server.held))
        if n < 1:
            return None
        return backlog / low, int(n)

    def _pick(self) -> tuple[Server, list[str]] | None:
        """The server and the batch of the next send, taken off the queue (``cv`` held)."""
        if not self.pending:
            return None
        now = time.monotonic()
        best = None
        for server in self.servers.values():
            r = self._room(server, now)
            if r is not None and (best is None or r[0] < best[1][0]):
                best = (server, r)
        if best is None:
            return None
        server, (_, room) = best
        keys = [self.pending.popleft() for _ in range(min(room, self.batch, len(self.pending)))]
        server.sent += len(keys)
        server.held.update(keys)
        for key in keys:
            self.jobs[key].tries += 1
            self.jobs[key].at = server
        if server.link is None:
            server.link = _Link(self, server)
            server.links += 1
        return server, keys

    def _dispatch(self) -> None:
        while True:
            with self.cv:
                while not self.stopping and (pick := self._pick()) is None:
                    self.cv.wait(0.2)
                    self._reap(time.monotonic())
                if self.stopping:
                    return
                server, keys = pick
                link = server.link
                lines = [self.jobs[k].line for k in keys]
            link.put(lines)

    def _reap(self, now: float) -> None:
        """A server silent for ``dead_s``: its link closed, its jobs back to the queue (``cv``
        held)."""
        for server in self.servers.values():
            if server.seen and now - server.seen > self.dead_s and server.link is not None:
                log.warning("%s silent: %d jobs back", server.name, len(server.held))
                link, server.link = server.link, None
                link.kill()
                self._back(server, set(server.held))

    def _link_down(self, link: _Link, exc: BaseException) -> None:
        server = link.server
        with self.cv:
            if server.link is not link:
                return
            server.link = None
            if server.held:
                log.warning(
                    "link to %s broke (%s): %d jobs back", server.name, exc, len(server.held)
                )
                server.down_until = time.monotonic() + min(self.dead_s, REFUSED_PAUSE_S)
            self._back(server, set(server.held))
        link.kill()

    def _answer(self, server: Server, answer: dict) -> None:
        """A server's answer line: to its poster, or (a refusal) the job back to the queue."""
        key = answer.get("key")
        with self.cv:
            if answer.get("refused"):
                server.down_until = time.monotonic() + min(self.dead_s, REFUSED_PAUSE_S)
                self._back(server, {key})
                return
            job = self.jobs.get(key)
            if job is None or job.at is not server:
                return
            del self.jobs[key]
            server.held.discard(key)
            ok = bool(answer.get("ok"))
            self.counts["answered" if ok else "failed"] += 1
            server.answered += ok
            server.failed += not ok
            self.cv.notify_all()
        job.req.lines.put({**answer, "server": server.name})
        self._settle(job.req)

    def _back(self, server: Server, keys: set[str]) -> None:
        """Jobs ``server`` held back to the front of the queue, or failed past ``tries`` (``cv``
        held)."""
        for key in keys:
            if key not in server.held:
                continue
            server.held.discard(key)
            job = self.jobs.get(key)
            if job is None or job.at is not server:
                continue
            job.at = None
            if job.tries >= self.tries:
                del self.jobs[key]
                self.counts["failed"] += 1
                job.req.lines.put({"key": key, "ok": False, "error": "no server ran it"})
                self._settle(job.req)
                continue
            self.pending.appendleft(key)
            self.counts["requeued"] += 1
        self.cv.notify_all()

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        now = time.monotonic()
        with self.cv:
            servers = {}
            for s in self.servers.values():
                load = s.status.get("load") or {}
                backlog, low, cap = self.marks(s)
                servers[s.name] = {
                    "url": s.url,
                    "up": self.up(s, now),
                    "rate": load.get("rate"),
                    "queued": load.get("queued"),
                    "resident": load.get("resident"),
                    "held": len(s.held),
                    "backlog": backlog,
                    "backlog_mark": round(low, 1),
                    "held_mark": None if cap is None else round(cap, 1),
                    "link": s.link is not None,
                    "links": s.links,
                    "sent": s.sent,
                    "answered": s.answered,
                    "failed": s.failed,
                    "seen_s_ago": None if not s.seen else round(now - s.seen, 2),
                }
            return {
                **self.counts,
                "pending": len(self.pending),
                "at_servers": len(self.jobs) - len(self.pending),
                "uptime_s": round(time.time() - self.started, 1) if self.started else 0.0,
                "servers": servers,
            }

    def allowed(self, headers: Any) -> bool:
        return token_ok(headers, self.token)


class _BalancerHandler(_Handler):
    service: Balancer

    def do_GET(self) -> None:
        if urlparse(self.path).path.rstrip("/") == "":
            if not self.service.allowed(self.headers):
                self._json(403, {"error": "missing or wrong token"})
                return
            routes = ["POST /jobs", "GET /status", "POST /servers/report"]
            self._json(200, {"routes": routes})
            return
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path.rstrip("/") != "/servers/report":
            super().do_POST()
            return
        if not self.service.allowed(self.headers):
            self._json(403, {"error": "missing or wrong token"})
            return
        try:
            body = json.loads(self._body() or b"{}")
            self.service.report(body)
        except (ValueError, KeyError, TypeError) as exc:
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, {"ok": True})
