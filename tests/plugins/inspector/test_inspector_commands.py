"""Commands the inspector contributes: ending event streams, replacing the access token."""

import socket
import threading
import time

from plugin_programs import PROGRAM
from test_plugin_inspector import get, post, raw

from conftest import build
from togeari.plugins import Inspector


def open_stream(port: int) -> socket.socket:
    """An open `/events` stream, past its response headers."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET /events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(1024)
    assert b" 200 " in head.split(b"\r\n")[0]
    return sock


def read_to_end(sock: socket.socket, ended: threading.Event) -> None:
    try:
        while sock.recv(4096):
            pass
    except OSError:
        return
    ended.set()


def test_the_contributed_commands_are_listed():
    insp = Inspector(port=0, token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        status, names = get(f"{insp.url}/commands")
        assert status == 200
        assert {"inspector_end_streams", "inspector_token"} <= set(names)
        assert m.inspect("inspector").result(5)["token_required"] is False
    finally:
        m.stop()


def test_ending_streams_closes_every_open_stream():
    insp = Inspector(port=0, token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    socks = []
    try:
        socks = [open_stream(insp.port) for _ in range(2)]
        ends = [threading.Event() for _ in socks]
        for s, e in zip(socks, ends, strict=True):
            threading.Thread(target=read_to_end, args=(s, e), daemon=True).start()
        assert m.inspect("inspector").result(5)["streams"] == 2

        # Both streams end within one keepalive, and the plugin forgets them
        assert m.command("inspector_end_streams").result(5) == 2
        assert all(e.wait(5) for e in ends)
        deadline = time.monotonic() + 5
        while m.inspect("inspector").result(5)["streams"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert m.inspect("inspector").result(5)["streams"] == 0

        # A stream opened afterwards stays open
        late = open_stream(insp.port)
        socks.append(late)
        late_end = threading.Event()
        threading.Thread(target=read_to_end, args=(late, late_end), daemon=True).start()
        assert not late_end.wait(1.5)
    finally:
        for s in socks:
            s.close()
        m.stop()


def test_replacing_the_token_changes_who_is_let_in():
    insp = Inspector(port=0, token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        host = {"Host": f"127.0.0.1:{insp.port}"}

        # A token set over HTTP is required from the next request on; it is never answered
        status, out = post(f"{insp.url}/commands/inspector_token", {"token": "s3cret"})
        assert status == 200 and out == {"token_required": True}
        assert raw(insp.port, "GET", "/queries/knobs", headers=host)[0] == 403
        auth = {**host, "Authorization": "Bearer s3cret"}
        status, info = raw(insp.port, "GET", "/queries/inspector", headers=auth)
        assert status == 200 and info["token_required"] is True and "s3cret" not in str(info)

        # An empty token is refused and the current one kept
        bad = {**auth, "Content-Type": "application/json"}
        body = b'{"token": ""}'
        assert raw(insp.port, "POST", "/commands/inspector_token", body, bad)[0] == 400
        assert insp.token == "s3cret"

        # None removes the token
        body = b'{"token": null}'
        assert raw(insp.port, "POST", "/commands/inspector_token", body, bad)[0] == 200
        assert raw(insp.port, "GET", "/queries/knobs", headers=host)[0] == 200
    finally:
        m.stop()
