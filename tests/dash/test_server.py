"""The dash server against a real machine with an inspector: run states, the proxy (GET, POST,
the relayed event stream) and its access rules. The reference for every change sent through the
proxy is the machine itself (its knobs, its units' states), read in-process."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("numpy")
pytest.importorskip("kohakuvault")

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.dash.server import accepted_hosts, serve
from togeari.plugins import Inspector
from togeari.plugins.telemetry import Telemetry
from togeari.testing import SimCall

LIB = NodeLibrary("dash-server")


@LIB.node()
def f(x: int) -> int:
    return x


PROGRAM = "@def (x: int)main(y):\n    (x)f(y)\n"


@pytest.fixture
def rig(tmp_path):
    """A started machine (telemetry with the inspector's token, the inspector) under
    ``tmp_path/runs/live``, and a dash server over ``tmp_path/runs``."""
    lane = Lane([SimCall(name="u0", latency_ms=1), SimCall(name="u1", latency_ms=1)], name="call")
    m = Machine(
        program=PROGRAM,
        entry="main",
        libraries=[LIB],
        bind={"f": Use(lane), "kir.std.*": Inline()},
        plugins=[
            Telemetry(tmp_path / "runs" / "live", writer="thread", inspector_token=True),
            Inspector(),
        ],
    )
    with m:
        # A query starts the dispatcher; telemetry then writes the inspector into machine.json
        m.inspect("stats").result(10)
        server = serve([tmp_path / "runs"], port=0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield m, f"http://127.0.0.1:{server.server_address[1]}", tmp_path
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


def call(url, data=None, headers=None, method=None):
    """(status, parsed JSON body) of a request; HTTP errors are answers too."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def post(url, body, **headers):
    return call(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})


def live_id(base):
    runs = call(f"{base}/api/runs")[1]
    return next(r["id"] for r in runs if r["name"] == "live")


def test_a_running_machine_is_live_and_its_token_is_never_served(rig):
    m, base, _ = rig
    runs = call(f"{base}/api/runs")[1]
    (row,) = [r for r in runs if r["name"] == "live"]
    assert row["state"] == "live"
    status, state = call(f"{base}/api/run/{row['id']}/state")
    assert status == 200 and state["state"] == "live"
    assert {"knob_specs", "jobs", "lanes"} <= set(state["queries"])
    assert {"set_knob", "detach"} <= set(state["commands"])
    token = m.plugins.get("inspector").token
    assert token
    for path in ("/api/runs", f"/api/run/{row['id']}/info", f"/api/run/{row['id']}/state"):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            assert token not in r.read().decode()


def test_queries_pass_through_with_their_arguments(rig):
    m, base, _ = rig
    rid = live_id(base)
    status, specs = call(f"{base}/api/run/{rid}/live/queries/knob_specs")
    assert status == 200 and specs["knobs"]["window"]["value"] == m.knobs["window"]
    status, lanes = call(f"{base}/api/run/{rid}/live/queries/lanes")
    assert status == 200 and [u["name"] for u in lanes[0]["units"]] == ["u0", "u1"]
    # An argument is parsed by the inspector as JSON: a job that is not resident is its error
    status, body = call(f"{base}/api/run/{rid}/live/queries/job?id=12345")
    assert status == 400 and "12345" in body["error"]


def test_a_command_through_the_proxy_changes_the_machine(rig):
    m, base, _ = rig
    rid = live_id(base)
    before = m.knobs["window"]
    status, answer = post(
        f"{base}/api/run/{rid}/live/commands/set_knob", {"name": "window", "value": 17}
    )
    assert status == 200 and answer == before
    assert m.knobs["window"] == 17
    status, body = post(
        f"{base}/api/run/{rid}/live/commands/set_knob", {"name": "window", "value": 0}
    )
    assert status == 400 and "window" in body["error"]
    assert m.knobs["window"] == 17


@pytest.mark.parametrize(
    "headers, status",
    [
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ({"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
        ({"Content-Type": "application/json", "Host": "evil.example"}, 403),
    ],
)
def test_a_command_another_page_could_send_is_refused_and_changes_nothing(rig, headers, status):
    m, base, _ = rig
    rid = live_id(base)
    body = json.dumps({"name": "window", "value": 9}).encode()
    got, answer = call(f"{base}/api/run/{rid}/live/commands/set_knob", body, headers, "POST")
    assert got == status and answer["error"]
    assert m.knobs["window"] != 9


def test_a_read_by_a_rebound_name_is_refused(rig):
    _, base, _ = rig
    assert call(f"{base}/api/runs", headers={"Host": "evil.example:80"})[0] == 403
    assert call(f"{base}/api/runs", headers={"Origin": "http://evil.example"})[0] == 403


def test_accepted_hosts():
    got = accepted_hosts("0.0.0.0", 8787, ["dash.lan", "proxy.lan:443"])
    assert {
        "localhost:8787",
        "127.0.0.1:8787",
        "[::1]:8787",
        "dash.lan:8787",
        "proxy.lan:443",
    } <= got
    assert "dash.lan:443" not in got and "proxy.lan:8787" not in got


def test_the_event_stream_is_relayed_as_it_happens(rig):
    m, base, _ = rig
    rid = live_id(base)
    req = urllib.request.Request(
        f"{base}/api/run/{rid}/live/events?kinds=unit.detach,unit.detached"
    )
    seen: list[dict] = []
    with urllib.request.urlopen(req, timeout=10) as stream:
        assert stream.headers["Content-Type"] == "text/event-stream"
        # The inspector subscribes when the stream opens; a keepalive proves it is open
        assert stream.readline().startswith(b": keepalive")
        stream.readline()
        status, _ = post(
            f"{base}/api/run/{rid}/live/commands/detach", {"unit": "u1", "drain": True}
        )
        assert status == 200
        kind = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(seen) < 2:
            line = stream.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                seen.append({"kind": kind, **json.loads(line[6:])})
    assert [e["kind"] for e in seen] == ["unit.detach", "unit.detached"]
    assert all(e["unit"] == "u1" for e in seen)
    states = {u["name"]: u["state"] for u in m.inspect("units").result(10)}
    assert states["u1"] == "detached" and states["u0"] == "up"


def test_a_stopped_machine_is_stopped_and_not_proxied(rig):
    m, base, _ = rig
    rid = live_id(base)
    m.stop()
    assert call(f"{base}/api/run/{rid}/state")[1]["state"] == "stopped"
    assert call(f"{base}/api/run/{rid}/live/queries/stats")[0] == 409


def test_a_run_whose_inspector_is_gone_is_unreachable(rig, tmp_path):
    _, base, _ = rig
    # A port nothing listens on: bound, then closed
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    dead = tmp_path / "runs" / "killed"
    dead.mkdir(parents=True)
    (dead / "machine.json").write_text(
        json.dumps(
            {
                "version": 1,
                "name": "killed",
                "started": time.time(),
                "inspector": f"http://127.0.0.1:{port}",
            }
        ),
        encoding="utf-8",
    )
    runs = call(f"{base}/api/runs")[1]
    (row,) = [r for r in runs if r["name"] == "killed"]
    assert row["state"] == "unreachable"
    status, body = call(f"{base}/api/run/{row['id']}/live/queries/stats")
    assert status == 502 and "unreachable" in body["error"]
