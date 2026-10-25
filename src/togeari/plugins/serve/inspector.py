"""The ``inspector`` plugin: a local HTTP endpoint for queries, commands and the event stream.

Routes:

- ``GET /``: the query and command names;
- ``GET /queries/<name>?arg=value``: a query answered by the dispatcher, as JSON
  (argument values are parsed as JSON, else kept as strings);
- ``POST /commands/<name>`` with a JSON object body: a command applied by the
  dispatcher, its result as JSON;
- ``GET /events?kinds=a,b``: Server-Sent Events, one ``event: <kind>`` /
  ``data: <json>`` message per bus event of those kinds (every kind without
  ``kinds``).

The plugin is subscribed to the bus only while a stream is open: at level
"counters" when every open stream asks only for always-delivered kinds, else
"all". With no stream open it costs the dispatcher nothing.

Access: the ``Host`` header must name the server (``localhost``, a loopback
address, the bound host, or one of ``allowed_hosts``) with its port; an
``Origin`` header, when present, must be ``http://`` such a host; a command
body must be sent as ``application/json``. Every request also carries the
access token as ``Authorization: Bearer <token>``, an ``X-Togeari-Token``
header or a ``token`` query argument. Without a ``token`` argument the plugin
makes a random one (``inspector.token``; ``inspector.access_url`` carries it);
``token=None`` turns the check off.

Contributed: the query ``inspector`` (url, open streams, dropped events, whether a token is
required) and the commands ``inspector_end_streams`` (ends every open event stream; the count)
and ``inspector_token`` (``{"token": str | None}``: replaces the access token).

Errors answer ``{"error": message}`` with 403 (access refused), 404 (unknown
name), 415 (command body not ``application/json``), 400 (failed, or a
``Content-Length`` that is not an integer >= 0), 413 (a body above
``max_body`` bytes) or 504 (no answer in time). A refused request's body is
drained up to ``DRAIN_BYTES`` and never parsed.
"""

import hmac
import json
import queue
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from togeari.core.plugin.base import BasePlugin, PluginContext
from togeari.plugins.common.machine import dumps

LOOPBACK = ("localhost", "127.0.0.1", "::1")

# Refused request bodies up to this size are read and discarded before the answer
DRAIN_BYTES = 64 * 1024

# The ``token`` default: a random token is made
GENERATE = object()


def _same(given: str, token: str) -> bool:
    """Constant-time comparison of two strings of any characters."""
    return hmac.compare_digest(given.encode("utf-8", "replace"), token.encode("utf-8", "replace"))


class _Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def _bracket(host: str) -> str:
    """A host as it appears in a URL: an IPv6 address in brackets."""
    bare = host.strip("[]")
    return f"[{bare}]" if ":" in bare else bare


def _arg(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


class _Handler(BaseHTTPRequestHandler):
    plugin: "Inspector"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _send(self, status: int, body: Any) -> None:
        data = dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _length(self) -> int | None:
        """The announced body length (0 without the header), or None when it is not an
        integer >= 0."""
        given = self.headers.get("Content-Length")
        if given is None:
            return 0
        try:
            length = int(given)
        except ValueError:
            return None
        return length if length >= 0 else None

    def _refuse(self, status: int, reason: str, length: int | None) -> None:
        """Answer a refused POST and close; a body up to ``DRAIN_BYTES`` is drained unparsed
        first, so the connection closes cleanly (a larger one is left and the connection
        dropped)."""
        self.close_connection = True
        if length is not None and 0 < length <= DRAIN_BYTES:
            self.rfile.read(length)
        self._send(status, {"error": reason})

    def _refused(self, query: dict) -> str | None:
        """Why the request is refused, or None."""
        hosts = self.plugin.hosts()
        if self.headers.get("Host", "").lower() not in hosts:
            return "unknown Host"
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower().removeprefix("http://") not in hosts:
            return "cross-origin request"
        token = self.plugin.token
        if token is None:
            return None
        auth = self.headers.get("Authorization", "")
        given = [
            auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None,
            self.headers.get("X-Togeari-Token"),
            (query.get("token") or [None])[-1],
        ]
        if not any(g is not None and _same(g, token) for g in given):
            return "missing or wrong token"
        return None

    def _answer(self, names: list[str], call: Any, name: str, args: dict) -> None:
        if name not in names:
            self._send(404, {"error": f"no such name {name!r}"})
            return
        try:
            self._send(200, call(name, **args).result(self.plugin.timeout_s))
        except TimeoutError as exc:
            self._send(504, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        refused = self._refused(query)
        if refused is not None:
            self._send(403, {"error": refused})
            return
        parts = [p for p in url.path.split("/") if p]
        ctx = self.plugin.ctx
        match parts:
            case []:
                self._send(200, {"queries": ctx.query_names(), "commands": ctx.command_names()})
            case ["queries"]:
                self._send(200, ctx.query_names())
            case ["commands"]:
                self._send(200, ctx.command_names())
            case ["queries", name]:
                args = {k: _arg(v[-1]) for k, v in query.items() if k != "token"}
                self._answer(ctx.query_names(), ctx.inspect, name, args)
            case ["events"]:
                given = (query.get("kinds") or [None])[-1]
                kinds = {k for k in given.split(",") if k} if given else None
                self._stream(kinds)
            case _:
                self._send(404, {"error": f"no route {url.path}"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]

        # Refused requests are answered from their headers; the body is never parsed
        length = self._length()
        refused = self._refused(parse_qs(url.query))
        if refused is not None:
            self._refuse(403, refused, length)
            return
        kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            self._refuse(415, "a command body is sent as application/json", length)
            return

        # The announced length: an integer >= 0, at most max_body
        cap = self.plugin.max_body
        if length is None:
            self._refuse(400, "Content-Length must be an integer >= 0", None)
            return
        if length > cap:
            self._refuse(413, f"a command body is at most {cap} bytes", length)
            return
        data = self.rfile.read(length)
        try:
            body = json.loads(data or b"{}")
        except ValueError as exc:
            self._send(400, {"error": f"body is not JSON: {exc}"})
            return
        match parts:
            case ["commands", name] if isinstance(body, dict):
                ctx = self.plugin.ctx
                self._answer(ctx.command_names(), ctx.command, name, body)
            case ["commands", _]:
                self._send(400, {"error": "body must be a JSON object"})
            case _:
                self._send(404, {"error": f"no route {self.path}"})

    def _stream(self, kinds: set[str] | None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        q = self.plugin.open_stream(kinds)
        try:
            while not self.plugin.closing.is_set() and not self.plugin.ended(q):
                try:
                    ev = q.get(timeout=1.0)
                    msg = f"event: {ev.kind}\ndata: {dumps(ev.to_dict())}\n\n"
                except queue.Empty:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.plugin.close_stream(q)


class Inspector(BasePlugin):
    """Serves queries, commands and events on ``host:port``.

    ``port=0`` (the default) picks a free port; ``port`` and ``url`` give the
    bound one once loaded. An IPv6 ``host`` (``"::1"``) is written ``[::1]`` in
    ``url`` and in accepted ``Host`` headers. ``token`` and ``allowed_hosts``
    set the access rules above: left out, ``token`` is a random string
    (``secrets.token_urlsafe``) read from ``inspector.token``; ``None`` turns the
    check off. ``max_body`` caps a command body in bytes.
    """

    name = "inspector"
    priority = 90
    events = None

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        timeout_s: float = 30.0,
        stream_queue: int = 1000,
        token: Any = GENERATE,
        allowed_hosts: tuple[str, ...] | list[str] = (),
        max_body: int = 1 << 20,
    ):
        self.host = host
        self.max_body = max_body
        self.port = port
        self.timeout_s = timeout_s
        self.stream_queue = stream_queue
        self.token: str | None = secrets.token_urlsafe(24) if token is GENERATE else token
        self.allowed_hosts = tuple(allowed_hosts)
        self.ctx: PluginContext | None = None
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.closing = threading.Event()
        self.streams: list[queue.Queue] = []
        # Each open stream's kinds (None: every kind), by id of its queue
        self._kinds: dict[int, set[str] | None] = {}
        self._ending: set[int] = set()
        self.dropped = 0
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://{_bracket(self.host)}:{self.port}"

    @property
    def access_url(self) -> str:
        """``url`` with the token as a query argument (``url`` when no token is required)."""
        if self.token is None:
            return self.url
        return f"{self.url}/?token={quote(self.token, safe='')}"

    def hosts(self) -> set[str]:
        """Accepted ``Host`` values: each accepted name with the bound port."""
        names = {*LOOPBACK, self.host, *self.allowed_hosts}
        return {f"{_bracket(n)}:{self.port}".lower() for n in names}

    def on_load(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self.closing.clear()
        handler = type("InspectorHandler", (_Handler,), {"plugin": self})
        server = _Server6 if ":" in self.host.strip("[]") else ThreadingHTTPServer
        self.server = server((self.host.strip("[]"), self.port), handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="togeari-inspector",
            daemon=True,
        )
        self.thread.start()

    def on_unload(self) -> None:
        self.closing.set()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def runtime_services(self) -> dict:
        return {"inspector": self}

    def contribute_queries(self) -> dict:
        return {"inspector": lambda m: self.status()}

    def contribute_commands(self) -> dict:
        return {
            "inspector_end_streams": lambda m: self.end_streams(),
            "inspector_token": lambda m, token=None: self.set_token(token),
        }

    def status(self) -> dict:
        with self._lock:
            return {
                "url": self.url,
                "streams": len(self.streams),
                "dropped": self.dropped,
                "token_required": self.token is not None,
            }

    # ------------------------------------------------------------ commands
    def end_streams(self) -> int:
        """Ends every open event stream within its next keepalive; the count ended."""
        with self._lock:
            self._ending.update(id(q) for q in self.streams)
            return len(self.streams)

    def set_token(self, token: str | None) -> dict:
        """Replaces the access token (None: no token); the token itself is never answered."""
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("token must be a non-empty string or None")
        self.token = token
        return {"token_required": token is not None}

    # ------------------------------------------------------------ event streams
    def open_stream(self, kinds: set[str] | None = None) -> queue.Queue:
        """A queue of the events of ``kinds`` (None: every kind); the subscription follows."""
        q: queue.Queue = queue.Queue(maxsize=self.stream_queue)
        with self._lock:
            self.streams.append(q)
            self._kinds[id(q)] = kinds
            self._subscribe()
        return q

    def close_stream(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self.streams:
                self.streams.remove(q)
            self._kinds.pop(id(q), None)
            self._ending.discard(id(q))
            self._subscribe()

    def _subscribe(self) -> None:
        """The subscription the open streams need (under the lock): none, "counters" when they
        ask only for always-delivered kinds, else "all"."""
        if self.ctx is None:
            return
        if not self.streams:
            self.ctx.unsubscribe()
        elif any(k is None for k in self._kinds.values()):
            self.ctx.subscribe(kinds=None)
        else:
            self.ctx.subscribe(kinds=set().union(*self._kinds.values()))

    def ended(self, q: queue.Queue) -> bool:
        with self._lock:
            return id(q) in self._ending

    def on_event(self, ev: Any) -> None:
        with self._lock:
            streams = [(q, self._kinds.get(id(q))) for q in self.streams]
        full = 0
        for q, kinds in streams:
            if kinds is not None and ev.kind not in kinds:
                continue
            try:
                q.put_nowait(ev)
            except queue.Full:
                full += 1
        if full:
            with self._lock:
                self.dropped += full
