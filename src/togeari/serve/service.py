"""A machine kept running as an HTTP service: jobs stream in, records stream back.

The machine runs one run that never drains: its source is a queue the HTTP handlers put jobs
into, so admission, backpressure and the issue policy hold exactly as for a file source. Each
record goes to the service's sink (when given, for keeping) and back to the request that sent
its job.

Routes:

- ``POST /jobs``: a stream of jobs. ``application/x-ndjson`` (or ``application/json``, one
  object or a list): each job ``{"key"?, "entry"?, "inputs": {...}}`` or whatever ``prepare``
  takes. Any other content type: the body is one job, handed to ``prepare`` as
  ``{"body": bytes, "content_type": str, **query arguments}``. The answer is
  ``application/x-ndjson``, chunked: one line per finished job, in finishing order, as
  ``present`` makes it (default ``{"key", "entry", "ok", "outputs", "failure", "stats"}``);
  a job refused before it runs answers ``{"key", "ok": false, "error"}``. Jobs are read while
  earlier ones run, and the answer ends when the body has ended and every job is answered.
- ``GET /status``: jobs in flight (queued or running), answered, failed, refused; whether the
  run is alive.
- ``GET /``: the routes.

A job's key is unique among the jobs in the service: a missing key is made
(``job-<n>``), a key already in the service is refused. A client that goes away leaves its
jobs running; their records still reach the sink.

Access: with a ``token``, every request carries ``Authorization: Bearer <token>`` or
``X-Togeari-Token``. Bound to a non-loopback host, the service answers anyone who can reach
it and has the token.
"""

import contextlib
import hmac
import itertools
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from togeari.core.contracts.io import JobRecord, SourceItem

log = logging.getLogger("togeari.serve")

NDJSON = "application/x-ndjson"
# The queue between the HTTP handlers and the machine's source reader: a full queue holds the
# handlers (and so the clients) back
QUEUE = 256
# An end marker: the source stops, the run drains
_END = object()


def default_present(record: JobRecord) -> dict:
    return {
        "key": record.key,
        "entry": record.entry,
        "ok": record.ok,
        "outputs": record.outputs,
        "failure": record.failure,
        "stats": record.stats,
    }


def default_prepare(job: dict) -> dict:
    if "body" in job:
        raise ValueError(f"no prepare for a {job.get('content_type') or 'raw'} body")
    if not isinstance(job.get("inputs"), dict):
        raise ValueError("a job is {'key'?, 'entry'?, 'inputs': {...}}")
    return job


class _Stream:
    """The run's source: items put by the handlers, until the end marker."""

    def __init__(self, size: int):
        self.q: queue.Queue = queue.Queue(maxsize=size)

    def __iter__(self):
        while (item := self.q.get()) is not _END:
            yield item


class _Request:
    """One POST's answers: records of its jobs, and how many are still owed."""

    def __init__(self):
        self.lines: queue.Queue = queue.Queue()
        self.owed = 0
        self.read_all = False
        self.lock = threading.Lock()


class Service:
    """``machine`` served on ``host:port`` (see the module's routes).

    ``sink``: also gets every record (``write`` / ``close``); ``prepare(job) -> {"key"?,
    "entry"?, "inputs"}`` turns a posted job into the machine's; ``present(record) -> dict``
    the answer line; ``plugins``: loaded into the machine before the run, in order (an
    inspector before a telemetry plugin that names it); ``queue`` bounds the jobs waiting for
    the source reader."""

    def __init__(
        self,
        machine: Any,
        sink: Any = None,
        prepare: Callable[[dict], dict] = default_prepare,
        present: Callable[[JobRecord], dict] = default_present,
        host: str = "127.0.0.1",
        port: int = 8790,
        token: str | None = None,
        plugins: tuple | list = (),
        queue_size: int = QUEUE,
    ):
        self.machine, self.sink = machine, sink
        self.prepare, self.present = prepare, present
        self.host, self.port, self.token = host, port, token
        self.plugins = list(plugins)
        self.stream = _Stream(queue_size)
        self.waiting: dict[str, _Request | None] = {}
        self.lock = threading.Lock()
        self.keys = itertools.count(1)
        self.counts = {"inflight": 0, "answered": 0, "failed": 0, "refused": 0}
        self.started = 0.0
        self.run_error: BaseException | None = None
        self.server: ThreadingHTTPServer | None = None
        self.runner: threading.Thread | None = None
        self.http: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> str:
        """Load the plugins, start the run and the HTTP server; the bound URL."""
        for plugin in self.plugins:
            self.machine.command("plugin_add", plugin=plugin).result()
        self.started = time.time()
        self.runner = threading.Thread(target=self._run, name="togeari-serve-run", daemon=True)
        self.runner.start()
        handler = type("ServiceHandler", (_Handler,), {"service": self})
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.http = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="togeari-serve-http",
            daemon=True,
        )
        self.http.start()
        return f"http://{self.host}:{self.port}"

    def stop(self, drain: bool = True, timeout: float = 600.0) -> None:
        """Stop taking jobs; with ``drain``, finish the queued and running ones first."""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if drain:
            self.stream.q.put(_END)
            if self.runner is not None:
                self.runner.join(timeout)
        self.machine.stop(drain=drain)

    def _run(self) -> None:
        try:
            self.machine.run(self.stream, self)
        except BaseException as exc:  # kept for /status, every waiter answered
            self.run_error = exc
            log.error("the service's run ended", exc_info=exc)
            with self.lock:
                waiting, self.waiting = self.waiting, {}
                self.counts["inflight"] -= len(waiting)
                self.counts["failed"] += len(waiting)
            for key, req in waiting.items():
                if req is not None:
                    req.lines.put({"key": key, "ok": False, "error": f"run ended: {exc!r}"})
                    self._settle(req)

    # ------------------------------------------------------------ the run's sink
    def write(self, record: JobRecord) -> None:
        if self.sink is not None:
            try:
                self.sink.write(record)
            except Exception:  # the answer still goes back
                log.exception("the service's sink failed on %s", record.key)
        with self.lock:
            req = self.waiting.pop(record.key, None)
            self.counts["inflight"] -= 1
            self.counts["answered" if record.ok else "failed"] += 1
        if req is not None:
            try:
                line = self.present(record)
            except Exception as exc:  # noqa: BLE001
                line = {"key": record.key, "ok": False, "error": f"present: {exc!r}"}
            req.lines.put(line)
            self._settle(req)

    def close(self) -> None:
        if self.sink is not None and hasattr(self.sink, "close"):
            self.sink.close()

    # ------------------------------------------------------------ jobs
    def submit(self, job: dict, req: _Request) -> None:
        """One posted job into the machine's source (blocking while the queue is full), or
        its refusal into ``req``."""
        key = job.get("key") if isinstance(job, dict) else None
        try:
            if self.run_error is not None:
                raise RuntimeError(f"the run ended: {self.run_error!r}")
            prepared = self.prepare(job)
            key = prepared.get("key")
            with self.lock:
                if key is None:
                    key = f"job-{next(self.keys)}"
                key = str(key)
                if key in self.waiting:
                    raise ValueError(f"key {key!r} is already in the service")
                self.waiting[key] = req
                self.counts["inflight"] += 1
            with req.lock:
                req.owed += 1
            item = SourceItem(key, dict(prepared["inputs"]), prepared.get("entry"))
            while True:
                try:
                    self.stream.q.put(item, timeout=1.0)
                    break
                except queue.Full:
                    if self.run_error is not None:
                        self._drop(key, req)
                        raise RuntimeError(f"the run ended: {self.run_error!r}") from None
        except Exception as exc:  # noqa: BLE001 - a refused job is answered, the stream goes on
            with self.lock:
                self.counts["refused"] += 1
            req.lines.put({"key": key, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _drop(self, key: str, req: _Request) -> None:
        """A registered job that never reached the queue, taken back."""
        with self.lock:
            if self.waiting.pop(key, None) is None:
                return
            self.counts["inflight"] -= 1
        with req.lock:
            req.owed -= 1

    def _settle(self, req: _Request) -> None:
        with req.lock:
            req.owed -= 1
            if req.read_all and req.owed == 0:
                req.lines.put(_END)

    def finish_reading(self, req: _Request) -> None:
        with req.lock:
            req.read_all = True
            if req.owed == 0:
                req.lines.put(_END)

    def status(self) -> dict:
        with self.lock:
            counts = dict(self.counts)
        alive = self.runner is not None and self.runner.is_alive()
        return {
            **counts,
            "running": alive,
            "error": None if self.run_error is None else repr(self.run_error),
            "uptime_s": round(time.time() - self.started, 1) if self.started else 0.0,
        }

    def allowed(self, headers: Any) -> bool:
        if self.token is None:
            return True
        auth = headers.get("Authorization", "")
        given = [auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None]
        given.append(headers.get("X-Togeari-Token"))
        return any(
            g is not None and hmac.compare_digest(g.encode(), self.token.encode()) for g in given
        )


class _Handler(BaseHTTPRequestHandler):
    service: Service
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, default=repr).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self.service.allowed(self.headers):
            self._json(403, {"error": "missing or wrong token"})
            return
        match urlparse(self.path).path.rstrip("/"):
            case "":
                self._json(200, {"routes": ["POST /jobs", "GET /status"]})
            case "/status":
                self._json(200, self.service.status())
            case path:
                self._json(404, {"error": f"no route {path}"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self.service.allowed(self.headers):
            self.close_connection = True
            self._json(403, {"error": "missing or wrong token"})
            return
        if url.path.rstrip("/") != "/jobs":
            self.close_connection = True
            self._json(404, {"error": f"no route {url.path}"})
            return
        kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        req = _Request()
        reader = threading.Thread(
            target=self._read, args=(kind, query, req), name="togeari-serve-read", daemon=True
        )
        reader.start()

        # Answers stream back while the body is still read
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", NDJSON)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        with contextlib.suppress(OSError):
            while (line := req.lines.get()) is not _END:
                data = (json.dumps(line, default=repr) + "\n").encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        reader.join()

    def _read(self, kind: str, query: dict, req: _Request) -> None:
        """Every job of the body into the service, then the request marked read."""
        try:
            for job in self._jobs(kind, query):
                self.service.submit(job, req)
        except Exception as exc:  # noqa: BLE001 - the body broke off: what was read still runs
            req.lines.put({"key": None, "ok": False, "error": f"body: {exc!r}"})
        finally:
            self.service.finish_reading(req)

    def _jobs(self, kind: str, query: dict):
        if kind == NDJSON:
            for raw in self._lines():
                if raw.strip():
                    yield json.loads(raw)
            return
        body = self._body()
        if kind == "application/json":
            parsed = json.loads(body or b"null")
            yield from parsed if isinstance(parsed, list) else [parsed]
            return
        yield {**query, "body": body, "content_type": kind}

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return b"".join(self._chunks())
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _lines(self):
        """The body's lines as they arrive (chunked or with a length)."""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            pending = b""
            for chunk in self._chunks():
                pending += chunk
                *lines, pending = pending.split(b"\n")
                yield from lines
            if pending:
                yield pending
            return
        left = int(self.headers.get("Content-Length") or 0)
        while left > 0:
            raw = self.rfile.readline(left)
            if not raw:
                return
            left -= len(raw)
            yield raw

    def _chunks(self):
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return
            data = self.rfile.read(size)
            self.rfile.readline()
            yield data
