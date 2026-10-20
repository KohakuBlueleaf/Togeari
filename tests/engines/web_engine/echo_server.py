"""A tiny local HTTP service for the WebCallEngine checks."""

import contextlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, body=None, headers=()):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
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
        body = json.loads(self.rfile.read(size) or b"null")
        match self.path:
            case "/double":
                self.reply(200, {"y": 2 * body["x"]})
            case "/slow":
                time.sleep(0.05)
                self.reply(200, {"y": 2 * body["x"]})
            case "/fail":
                self.reply(500, {"error": "boom"})
            case "/busy":
                self.reply(429, {"error": "busy"}, [("Retry-After", "1")])
            case "/down":
                self.reply(503, {"error": "down"})
            case _:
                self.reply(404, {"error": self.path})


class Httpd(ThreadingHTTPServer):
    """Tracks open connections so ``close`` can drop keep-alive ones too."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args):
        super().__init__(*args)
        self.conns = set()

    def process_request(self, request, client_address):
        self.conns.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        self.conns.discard(request)
        super().shutdown_request(request)


class Server:
    """The service on a free local port, served from a daemon thread."""

    def __init__(self):
        self.httpd = Httpd(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.httpd.shutdown()
        self.httpd.server_close()
        for conn in list(self.httpd.conns):
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
