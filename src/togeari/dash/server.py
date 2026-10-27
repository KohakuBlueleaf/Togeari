"""``togeari dash``: the dashboard's server over telemetry directories.

A separate process from the machine: it reads each run's directory read-only (``query.Run``)
and, while the run is live, forwards ``/api/run/<id>/live/...`` to the machine's inspector
with the inspector's token, so the page and its API share one origin.

Routes (all GET unless noted; an entity is ``kind:id``, e.g. ``node:12``, ``lane:1``):

- ``/api/runs``: every run under the roots (a directory holding ``machine.json``), each with its
  ``state``: ``live`` (no ``stopped`` time and its inspector answers), ``unreachable`` (no
  ``stopped`` time, no inspector answering: a machine killed, or one without an inspector) or
  ``stopped``;
- ``/api/run/<id>/state``: that state, with the inspector's query and command names when live;
- ``/api/run/<id>/info``: ``machine.json``, the time span, the name table, series names and
  the machine record;
- ``/api/run/<id>/programs``, ``/api/run/<id>/program/<start>``;
- ``/api/run/<id>/table?kind=&t0=&t1=``: each entity's metrics over a range;
- ``/api/run/<id>/series?entity=&metric=&t0=&t1=&points=&stat=`` (or ``step=``: buckets at
  multiples of it);
- ``/api/run/<id>/series_each?entities=a,b&metric=&t0=&t1=&step=&stat=&since=``: one list of
  those series, read together, from ``since`` (a refresh's last bucket) on;
- ``/api/run/<id>/fine?pairs=entity/metric,...&t0=&t1=&since=&columns=n,sum,lo,hi``: the
  finest stored rows of each pair (``Run.fine``) from ``since`` on, undivided, the chosen
  columns as float32 (``fine_body``), gzipped when accepted;
- ``/api/run/<id>/top?kind=&metric=&t0=&t1=&stat=&k=``;
- ``/api/run/<id>/hist?entity=&metric=&t0=&t1=``;
- ``/api/run/<id>/samples?names=a,b&prefix=p/,q/&t0=&t1=``: every sample; with
  ``points=`` (and ``chunks=``, and for a refresh ``step=&since=``) the bucketed series
  streamed newest first as JSON lines (``Run.sample_chunks``);
- ``/api/run/<id>/traces?t0=&t1=&reason=&limit=``, ``/api/run/<id>/trace?key=``;
- ``/api/run/<id>/records?t0=&t1=&limit=``: raw records;
- ``/api/run/<id>/live/<path>`` (GET, POST): the inspector's ``/<path>``; ``live/events`` is
  relayed as it streams (Server-Sent Events) until either side closes;
- anything else: the built app (``static/``), ``index.html`` for unknown paths.

Access: the ``Host`` header must name the server (a loopback name, the bound host, or one of
``allowed_hosts``) with its port, and an ``Origin`` header, when present, ``http://`` such a
host; a POST body is sent as ``application/json``. Other pages in the browser therefore cannot
send commands through the proxy (a cross-origin JSON POST needs a preflight this server never
grants) nor read the API by rebinding a name to the loopback address.
"""

import gzip
import hashlib
import json
import logging
import mimetypes
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from togeari.plugins.common.machine import json_default
from togeari.plugins.telemetry import layout as L
from togeari.plugins.telemetry.query import Run, record_dict

log = logging.getLogger("togeari.dash")

STATIC = Path(__file__).parent / "static"
MAX_BODY = 1 << 20
# How long a liveness probe waits for the inspector, and how long a relayed event stream waits
# for its next bytes (the inspector sends a keepalive every second)
PROBE_S = 1.0
STREAM_IDLE_S = 15.0
GZIP_LEVEL = 5
FINE_COLUMNS = ("n", "sum", "lo", "hi")


def entity(text: str) -> int:
    kind, _, ident = text.partition(":")
    return L.entity(L.ENTITY_KINDS[kind], int(ident))


def entity_text(e: int) -> str:
    kinds = {v: k for k, v in L.ENTITY_KINDS.items()}
    return f"{kinds[L.entity_kind(e)]}:{L.entity_id(e)}"


class Runs:
    """The runs under the roots, found again on every listing; ids are stable per path."""

    def __init__(self, roots: list[Path], token: str | None = None):
        """``token``: the inspector's token for every run (else each ``machine.json``'s)."""
        self.roots, self.token = roots, token
        self.by_id: dict[str, Path] = {}
        self.lock = threading.Lock()

    def find(self) -> tuple[list[dict], dict[str, dict]]:
        """Every run under the roots with its ``machine.json``, not probed."""
        found, raw = [], {}
        for root in self.roots:
            for info in sorted(root.glob("**/machine.json")):
                path = info.parent
                rid = f"{path.name}-{hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:8]}"
                try:
                    data = json.loads(info.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                with self.lock:
                    self.by_id[rid] = path
                raw[rid] = data
                found.append({"id": rid, "path": str(path), **public(data)})
        return found, raw

    def scan(self) -> list[dict]:
        found, raw = self.find()
        # Every run that claims to be live is probed at once
        claims = [rid for rid, data in raw.items() if not data.get("stopped")]
        states: dict[str, str] = {}

        def check(rid: str) -> None:
            states[rid] = probe(raw[rid], self.token)[0]

        threads = [threading.Thread(target=check, args=(rid,)) for rid in claims]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for r in found:
            r["state"] = states.get(r["id"], "stopped")
        return sorted(found, key=lambda r: -(r.get("started") or 0))

    def get(self, rid: str) -> Run | None:
        with self.lock:
            path = self.by_id.get(rid)
        if path is None:
            self.find()
            with self.lock:
                path = self.by_id.get(rid)
        return None if path is None else Run(path)


def fine_body(
    t0: float, t1: float, series: list[list[dict]], columns: tuple = FINE_COLUMNS
) -> bytes:
    """``Run.fine`` as bytes: a little-endian u32 header length, the JSON header (``t0``,
    ``t1``, ``columns``, per pair its pieces ``{tier, period, start, len}``) padded with
    spaces to a multiple of 4, then per pair, per piece, each of ``columns`` as float32."""
    header = {
        "t0": t0,
        "t1": t1,
        "columns": columns,
        "series": [
            [{k: p[k] for k in ("tier", "period", "start")} | {"len": len(p["n"])} for p in pieces]
            for pieces in series
        ],
    }
    head = json.dumps(header).encode()
    head += b" " * (-len(head) % 4)
    cols = [p[c].astype("<f4").tobytes() for pieces in series for p in pieces for c in columns]
    return b"".join([len(head).to_bytes(4, "little"), head, *cols])


def public(info: dict) -> dict:
    """``machine.json`` without the inspector's token."""
    return {k: v for k, v in info.items() if k != "inspector_token"}


def _request(info: dict, path: str, token: str | None, data: bytes | None = None):
    """A request to the run's inspector, carrying its token."""
    req = urllib.request.Request(info["inspector"] + path, data=data)
    token = token or info.get("inspector_token")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return req


def probe(info: dict, token: str | None = None) -> tuple[str, dict]:
    """The run's state and, when live, the inspector's query and command names."""
    if info.get("stopped"):
        return "stopped", {}
    if not info.get("inspector"):
        return "unreachable", {}
    try:
        with urllib.request.urlopen(_request(info, "/", token), timeout=PROBE_S) as r:
            return "live", json.loads(r.read())
    except (OSError, ValueError):
        return "unreachable", {}


def _f(q: dict, name: str, default: float | None) -> float | None:
    v = q.get(name)
    return float(v) if v not in (None, "") else default


class _Handler(BaseHTTPRequestHandler):
    runs: Runs
    token: str | None
    hosts: set[str]
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _refused(self) -> str | None:
        """Why the request is refused (its Host or Origin is not this server), or None."""
        if self.headers.get("Host", "").lower() not in self.hosts:
            return "unknown Host"
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower().removeprefix("http://") not in self.hosts:
            return "cross-origin request"
        return None

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Any) -> None:
        """``value`` as strict JSON; a non-finite float is the server's error (500)."""
        try:
            body = json.dumps(value, default=json_default, allow_nan=False)
        except ValueError as exc:
            log.warning("dash: %s returned a non-finite number", self.path)
            status, body = 500, json.dumps({"error": f"non-finite number in the reply: {exc}"})
        self._send(status, body.encode(), "application/json")

    def _binary(self, body: bytes) -> None:
        """Bytes, gzipped when the client takes gzip."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        if "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=GZIP_LEVEL)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _ndjson(self, values: Any) -> None:
        """Each of ``values`` as one JSON line, sent as it is made (chunked); a failure while
        streaming ends the stream with an ``{"error"}`` line. A client gone stops it."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def put(value: Any) -> None:
            line = json.dumps(value, default=json_default, allow_nan=False).encode() + b"\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            self.wfile.flush()

        try:
            try:
                for value in values:
                    put(value)
            except OSError:
                raise
            except Exception as exc:
                log.warning("dash: %s failed while streaming", self.path, exc_info=True)
                put({"error": f"{type(exc).__name__}: {exc}"})
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            self.close_connection = True

    def do_GET(self) -> None:
        refused = self._refused()
        if refused is not None:
            self._json(403, {"error": refused})
            return
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if parts[:1] != ["api"]:
            self._static(url.path)
            return
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            self._api(parts[1:], q)
        except (KeyError, ValueError) as exc:
            self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            log.warning("dash: %s failed", self.path, exc_info=True)
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:
        self.close_connection = True
        refused = self._refused()
        kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if refused is None and kind != "application/json":
            refused = "a command body is sent as application/json"
        if refused is not None:
            # The body is read unparsed first: closing with it unread resets the connection
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY:
                self.rfile.read(length)
            self._json(403 if kind == "application/json" else 415, {"error": refused})
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:2] == ["api", "run"] and len(parts) >= 4 and parts[3] == "live":
            self._live(parts[2], "/" + "/".join(parts[4:]))
            return
        self._json(404, {"error": "no route"})

    # ------------------------------------------------------------ the API
    def _api(self, parts: list[str], q: dict) -> None:
        if parts == ["runs"]:
            self._json(200, self.runs.scan())
            return
        if len(parts) < 3 or parts[0] != "run":
            self._json(404, {"error": "no route"})
            return
        run = self.runs.get(parts[1])
        if run is None:
            self._json(404, {"error": f"no run {parts[1]!r}"})
            return
        if parts[2] == "live":
            self._live(parts[1], "/" + "/".join(parts[3:]), self.path.partition("?")[2])
            return
        if parts[2:] == ["state"]:
            state, names = probe(run.info(), self.token)
            self._json(200, {"state": state, **names})
            return
        span = run.span() or (0.0, 0.0)
        t0, t1 = _f(q, "t0", span[0]), _f(q, "t1", span[1])
        match parts[2:]:
            case ["info"]:
                self._json(
                    200,
                    {
                        **public(run.info()),
                        "span": run.span(),
                        "names": run.names(),
                        "series": run.series_names(),
                        "machine": run.static("machine"),
                    },
                )
            case ["programs"]:
                self._json(200, run.programs())
            case ["program", start]:
                self._json(200, run.program(int(start)))
            case ["table"]:
                rows = run.table(q.get("kind", "node"), t0, t1)
                self._json(200, [{**r, "entity": entity_text(r["entity"])} for r in rows])
            case ["series"]:
                self._json(
                    200,
                    run.series(
                        entity(q["entity"]),
                        q.get("metric", "calls"),
                        t0,
                        t1,
                        int(q.get("points", 600)),
                        q.get("stat", "rate"),
                        _f(q, "step", None),
                    ),
                )
            case ["series_each"]:
                # A refresh reads from `since` (the start of the last bucket it holds) on
                self._json(
                    200,
                    run.series_each(
                        [entity(e) for e in q.get("entities", "").split(",") if e],
                        q.get("metric", "calls"),
                        max(t0, _f(q, "since", t0)),
                        t1,
                        stat=q.get("stat", "rate"),
                        step=float(q["step"]),
                    ),
                )
            case ["fine"]:
                pairs = [p.rsplit("/", 1) for p in q.get("pairs", "").split(",") if p]
                # A refresh reads from `since` (the start of the last row it holds) on
                a = max(t0, _f(q, "since", t0))
                columns = tuple(c for c in q.get("columns", "").split(",") if c) or FINE_COLUMNS
                if not set(columns) <= set(FINE_COLUMNS):
                    raise ValueError(f"columns are among {FINE_COLUMNS}")
                got = run.fine([(entity(e), m) for e, m in pairs], a, t1)
                self._binary(fine_body(a, t1, got, columns))
            case ["top"]:
                rows = run.top(
                    q.get("kind", "node"),
                    q.get("metric", "service"),
                    t0,
                    t1,
                    q.get("stat", "p90"),
                    int(q.get("k", 10)),
                )
                self._json(200, [{**r, "entity": entity_text(r["entity"])} for r in rows])
            case ["hist"]:
                self._json(200, run.hist(entity(q["entity"]), q.get("metric", "service"), t0, t1))
            case ["samples"]:
                names = [n for n in q.get("names", "").split(",") if n]
                prefixes = tuple(p for p in q.get("prefix", "").split(",") if p)
                if prefixes:
                    known = [v["name"] for v in run.series_names().values()]
                    names += [n for n in known if n.startswith(prefixes)]
                if "points" not in q:
                    self._json(200, run.samples(names, t0, t1))
                    return
                chunks = run.sample_chunks(
                    names,
                    t0,
                    t1,
                    int(q["points"]),
                    int(q.get("chunks", 8)),
                    _f(q, "since", None),
                    int(q["step"]) if q.get("step") else None,
                )
                self._ndjson(chunks)
            case ["traces"]:
                self._json(
                    200,
                    run.traces(
                        _f(q, "t0", 0.0),
                        _f(q, "t1", float("inf")),
                        q.get("reason") or None,
                        int(q.get("limit", 200)),
                    ),
                )
            case ["trace"]:
                got = run.trace(q["key"])
                self._json(200 if got is not None else 404, got or {"error": "no trace"})
            case ["records"]:
                epoch = run.info()["epoch_wall"]
                recs = run.raw(t0, t1, int(q.get("limit", 20000)))
                self._json(200, [record_dict(r, epoch) for r in recs])
            case _:
                self._json(404, {"error": "no route"})

    # ------------------------------------------------------------ the live machine
    def _live(self, rid: str, path: str, query: str = "") -> None:
        """Forward to the run's inspector, with its token (``--token`` or ``machine.json``)."""
        run = self.runs.get(rid)
        info = run.info() if run is not None else {}
        if not info.get("inspector") or info.get("stopped"):
            self._json(409, {"error": "the run is not live"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, MAX_BODY)) if self.command == "POST" else None
        req = _request(info, path + (f"?{query}" if query else ""), self.token, body)
        stream = path == "/events"
        try:
            with urllib.request.urlopen(req, timeout=STREAM_IDLE_S if stream else 30) as r:
                if stream:
                    self._relay(r)
                    return
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read(), "application/json")
        except OSError as e:
            self._json(502, {"error": f"inspector unreachable: {e}"})

    def _relay(self, upstream: Any) -> None:
        """The inspector's event stream, passed on as its bytes arrive, until either side
        closes (or the inspector is silent ``STREAM_IDLE_S``: its keepalives stopped)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while chunk := upstream.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass

    # ------------------------------------------------------------ the app
    def _static(self, path: str) -> None:
        root = STATIC.resolve()
        target = (root / path.lstrip("/")).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            target = root / "index.html"
        if not target.is_file():
            self._send(404, b"the dashboard app is not built (apps/dashboard)", "text/plain")
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), kind)


def accepted_hosts(host: str, port: int, allowed: list[str] | tuple[str, ...] = ()) -> set[str]:
    """The ``Host`` / ``Origin`` values accepted: each loopback name, the bound host and every
    ``allowed`` name with the bound port; an ``allowed`` entry with its own port is kept as is."""
    names = {"localhost", "127.0.0.1", "[::1]", host, *(a for a in allowed if ":" not in a)}
    return {f"{n}:{port}".lower() for n in names} | {a.lower() for a in allowed if ":" in a}


def serve(
    roots: list[Path],
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
    allowed_hosts: list[str] | tuple[str, ...] = (),
) -> ThreadingHTTPServer:
    """A started server (``serve_forever`` on its own thread is the caller's)."""
    handler = type("DashHandler", (_Handler,), {"runs": Runs(roots, token), "token": token})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    handler.hosts = accepted_hosts(host, server.server_address[1], allowed_hosts)
    return server
