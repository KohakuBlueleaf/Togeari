"""Inspector: queries, commands and the event stream over HTTP."""

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
from plugin_programs import PROGRAM, jobs

from conftest import build, run_jobs
from togeari.plugins import Inspector


def get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def post(url: str, body: dict) -> tuple[int, object]:
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def raw(port: int, method: str, path: str, body: bytes = b"", headers: dict | None = None):
    """(status, decoded body) of one request with exactly the given headers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    resp = conn.getresponse()
    status, data = resp.status, json.loads(resp.read())
    conn.close()
    return status, data


def test_inspector_refuses_cross_site_and_foreign_host_requests():
    insp = Inspector(port=0, token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        port = insp.port
        assert port != 0 and insp.url.endswith(f":{port}")
        host = {"Host": f"127.0.0.1:{port}"}
        body = json.dumps({"name": "inline_warn_us", "value": 7}).encode()
        knob = ("POST", "/commands/set_knob", body)

        # A simple cross-site form post: wrong content type, foreign origin, no change.
        form = {**host, "Content-Type": "text/plain", "Origin": "http://evil.example"}
        assert raw(port, *knob, form)[0] == 403
        assert raw(port, *knob, {**host, "Content-Type": "text/plain"})[0] == 415

        # A rebound name reaching the port: refused by the Host check, reads included.
        rebound = {"Host": f"evil.example:{port}", "Content-Type": "application/json"}
        assert raw(port, *knob, rebound)[0] == 403
        assert (
            raw(port, "GET", "/queries/knobs", headers={"Host": f"evil.example:{port}"})[0] == 403
        )
        assert m.inspect("knobs").result()["inline_warn_us"] == 50

        # The same command from the local host as JSON goes through.
        local = {"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}
        status, old = raw(port, *knob, {**local, "Content-Type": "application/json"})
        assert status == 200 and old == 50
        assert m.inspect("knobs").result()["inline_warn_us"] == 7
    finally:
        m.stop()


def test_inspector_token():
    insp = Inspector(port=0, token="s3cret")
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        port = insp.port
        host = {"Host": f"127.0.0.1:{port}"}
        assert raw(port, "GET", "/queries/knobs", headers=host)[0] == 403
        bearer = {**host, "Authorization": "Bearer s3cret"}
        assert raw(port, "GET", "/queries/knobs", headers=bearer)[0] == 200
        assert raw(port, "GET", "/queries/knobs?token=s3cret", headers=host)[0] == 200
        assert raw(port, "GET", "/queries/knobs?token=wrong", headers=host)[0] == 403
        assert insp.token not in json.dumps(raw(port, "GET", "/queries/inspector", headers=bearer))

        # Non-ASCII tokens and headers are compared, not dropped
        accented = {**host, "Authorization": "Bearer s3cr\xe9t"}
        status, reply = raw(port, "GET", "/queries/knobs", headers=accented)
        assert status == 403 and reply == {"error": "missing or wrong token"}
    finally:
        m.stop()


def test_inspector_defaults_to_a_free_port_and_a_random_token():
    insp = Inspector()
    other = Inspector()
    assert insp.port == 0
    assert isinstance(insp.token, str) and len(insp.token) >= 24 and insp.token != other.token
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        assert insp.port != 0 and insp.url == f"http://127.0.0.1:{insp.port}"

        # Without the token: refused; with it (as access_url carries it): answered
        assert get(insp.url + "/queries/knobs")[0] == 403
        assert get(f"{insp.url}/queries/knobs?token={insp.token}")[0] == 200
        assert get(insp.access_url)[0] == 200
        assert m.inspect("inspector").result(5)["token_required"] is True
    finally:
        m.stop()


def test_token_none_serves_without_a_token():
    insp = Inspector(token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        assert insp.token is None and insp.access_url == insp.url
        assert get(insp.url + "/queries/knobs")[0] == 200
    finally:
        m.stop()


def test_inspector_refuses_a_command_before_reading_its_body():
    insp = Inspector()
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        # Headers announce a large body that never comes: the refusal cannot wait for it.
        sock = socket.create_connection(("127.0.0.1", insp.port), timeout=5)
        head = (
            "POST /commands/set_knob HTTP/1.1\r\n"
            f"Host: evil.example:{insp.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 10000000\r\n\r\n"
        )
        sock.sendall(head.encode())
        reply = sock.recv(4096).decode()
        sock.close()
        assert reply.startswith("HTTP/1.1 403"), reply
    finally:
        m.stop()


def raw_head(port: int, length: str, body: bytes = b"") -> str:
    """The status line of a JSON command whose Content-Length header is ``length`` as given."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    head = (
        "POST /commands/set_knob HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n"
    )
    sock.sendall(head.encode() + body)
    reply = sock.recv(4096).decode()
    sock.close()
    return reply.split("\r\n", 1)[0]


def test_inspector_checks_the_announced_body_length():
    insp = Inspector(max_body=100, token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        port = insp.port
        body = json.dumps({"name": "inline_warn_us", "value": 9}).encode()

        # Not an integer, negative: 400 at once, nothing read
        assert raw_head(port, "ten").startswith("HTTP/1.1 400")
        assert raw_head(port, "-1").startswith("HTTP/1.1 400")

        # Above the cap: 413, the knob unchanged
        big = json.dumps({"name": "inline_warn_us", "value": 9, "pad": "x" * 200}).encode()
        assert raw_head(port, str(len(big)), big).startswith("HTTP/1.1 413")
        assert m.inspect("knobs").result()["inline_warn_us"] == 50

        # Within the cap: applied
        assert raw_head(port, str(len(body)), body).startswith("HTTP/1.1 200")
        assert m.inspect("knobs").result()["inline_warn_us"] == 9
    finally:
        m.stop()


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback(), reason="no IPv6 loopback")
def test_inspector_serves_an_ipv6_host():
    insp = Inspector(host="::1", token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        assert insp.url == f"http://[::1]:{insp.port}"
        conn = http.client.HTTPConnection("::1", insp.port, timeout=10)
        conn.request("GET", "/queries/knobs", headers={"Host": f"[::1]:{insp.port}"})
        resp = conn.getresponse()
        assert resp.status == 200 and "inline_warn_us" in json.loads(resp.read())
        conn.close()
    finally:
        m.stop()


def read_stream(port: int, kinds: list, stop: threading.Event) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/events")
    resp = conn.getresponse()
    while not stop.is_set():
        line = resp.fp.readline().decode()
        if not line:
            break
        if line.startswith("event: "):
            kinds.append(line[len("event: ") :].strip())
    conn.close()


def test_inspector_queries_commands_and_events():
    insp = Inspector(port=0, token=None)
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        base = insp.url
        assert m.inspect("inspector").result()["url"] == base

        # Names, a query, an unknown query, a failing query
        status, names = get(base + "/")
        assert status == 200 and "lanes" in names["queries"] and "set_knob" in names["commands"]
        status, lanes = get(base + "/queries/lanes")
        assert status == 200 and {ln["name"] for ln in lanes} == {"call", "batch"}
        assert get(base + "/queries/nope")[0] == 404
        status, err = get(base + "/queries/job?id=999999")
        assert status == 400 and "KeyError" in err["error"]

        # A command changes a knob; the knobs query shows it
        status, old = post(base + "/commands/set_knob", {"name": "inline_warn_us", "value": 123})
        assert status == 200 and old == 50
        assert get(base + "/queries/knobs")[1]["inline_warn_us"] == 123
        assert post(base + "/commands/nope", {})[0] == 404

        # The event stream carries the jobs' events
        kinds: list[str] = []
        stop = threading.Event()
        reader = threading.Thread(target=read_stream, args=(insp.port, kinds, stop), daemon=True)
        reader.start()
        deadline = time.monotonic() + 10
        while not insp.streams and time.monotonic() < deadline:
            time.sleep(0.01)
        sink = run_jobs(m, jobs(6))
        deadline = time.monotonic() + 10
        while kinds.count("job.end") < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        assert len(sink.records) == 6
        assert kinds.count("job.admit") == 6 and kinds.count("job.end") == 6
    finally:
        m.stop()

    # The server is gone after the machine stops
    try:
        urllib.request.urlopen(base + "/", timeout=2)
        reachable = True
    except OSError:
        reachable = False
    assert not reachable
