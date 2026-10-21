"""A local station registry over HTTP: ``POST /lookup`` gives a station's gain, ``GET /health``.

Each lookup takes 20-80 ms (fixed per station); stations outside the registry get 404.
"""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def known(station: str) -> bool:
    return not station.endswith("7")


def gain(station: str) -> float:
    h = hashlib.sha256(station.encode()).digest()
    return 0.5 + h[0] / 255.0


def delay_s(station: str) -> float:
    h = hashlib.sha256(station.encode()).digest()
    return 0.02 + 0.06 * h[1] / 255.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        match self.path:
            case "/health":
                self.reply(200, {"ok": True})
            case _:
                self.reply(404, {"error": self.path})

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(size) or b"{}")
        match self.path, body.get("station"):
            case "/lookup", str(station) if known(station):
                time.sleep(delay_s(station))
                self.reply(200, {"station": station, "gain": gain(station)})
            case "/lookup", station:
                time.sleep(delay_s(str(station)))
                self.reply(404, {"error": f"unknown station {station!r}"})
            case _:
                self.reply(404, {"error": self.path})


class Server(ThreadingHTTPServer):
    # Every station's lookup connects at once: with the default backlog of 5, Windows refuses
    # the connects past it and the client retries them 0.5 s later
    request_queue_size = 256


class Registry:
    """The service on a free local port, served from a daemon thread."""

    def __init__(self):
        self.httpd = Server(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
