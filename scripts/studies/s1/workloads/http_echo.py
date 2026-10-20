"""A local HTTP echo service in its own process (``EchoService``).

Routes:

- ``POST /echo``: a JSON body, answered with the same JSON;
- ``POST /sink``: a pickled body, unpickled; answers ``{"t": perf_counter()}`` once decoded;
- ``POST /mirror``: a pickled body, unpickled and pickled back;
- ``GET /cpu``: ``{"cpu": process_time()}`` of the service process.

``lib`` holds the signature of the ``call`` node, whose calls go to ``/echo``.
"""

import json
import multiprocessing
import pickle
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kohakunode import NodeLibrary

_SPAWN = multiprocessing.get_context("spawn")

lib = NodeLibrary("s1web")


@lib.node()
def call(x: int, pad: str) -> int:
    """The signature of the ``call`` node; its calls run on a ``WebCallEngine``."""
    raise NotImplementedError("call runs on a WebCallEngine")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def reply(self, data: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        match self.path:
            case "/cpu":
                self.reply(json.dumps({"cpu": time.process_time()}).encode(), "application/json")
            case _:
                self.send_error(404)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        match self.path:
            case "/echo":
                data = json.dumps(json.loads(body)).encode()
                self.reply(data, "application/json")
            case "/sink":
                pickle.loads(body)
                t = time.perf_counter()
                self.reply(json.dumps({"t": t}).encode(), "application/json")
            case "/mirror":
                data = pickle.dumps(pickle.loads(body), protocol=5)
                self.reply(data, "application/octet-stream")
            case _:
                self.send_error(404)


class Httpd(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def serve(conn: Any) -> None:
    """The service process: bind a free local port, report it, serve until killed."""
    httpd = Httpd(("127.0.0.1", 0), Handler)
    conn.send(httpd.server_address[1])
    conn.close()
    httpd.serve_forever()


class EchoService:
    """The echo service in a spawned process; ``url`` once started."""

    def __init__(self, start_timeout_s: float = 30.0):
        self.start_timeout_s = start_timeout_s
        self.proc: Any = None
        self.url = ""

    def start(self) -> "EchoService":
        here, there = _SPAWN.Pipe(duplex=False)
        self.proc = _SPAWN.Process(target=serve, args=(there,), name="s1-echo", daemon=True)
        self.proc.start()
        there.close()
        if not here.poll(self.start_timeout_s):
            self.close()
            raise TimeoutError("echo service did not start")
        self.url = f"http://127.0.0.1:{here.recv()}"
        here.close()
        return self

    def close(self) -> None:
        if self.proc is not None and self.proc.is_alive():
            self.proc.kill()
        if self.proc is not None:
            self.proc.join(5.0)

    def __enter__(self) -> "EchoService":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()
