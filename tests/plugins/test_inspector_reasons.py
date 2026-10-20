"""Inspector refusals: each refused read names its reason; each accepted form of a token works."""

import http.client
import json

import pytest
from plugin_programs import PROGRAM

from conftest import build
from togeari.plugins import Inspector


def get(port: int, path: str, headers: dict) -> tuple[int, dict]:
    """(status, decoded body) of one GET with exactly the given headers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    status, data = resp.status, json.loads(resp.read())
    conn.close()
    return status, data


@pytest.fixture
def served():
    """A machine with an inspector that requires the token ``s3cret``."""
    insp = Inspector(token="s3cret")
    m = build(PROGRAM, "main", plugins=[insp])
    try:
        yield insp.port
    finally:
        m.stop()


def test_refused_reads_name_their_reason(served):
    port = served
    host = {"Host": f"127.0.0.1:{port}", "X-Togeari-Token": "s3cret"}
    cases = [
        ({"X-Togeari-Token": "s3cret"}, "unknown Host"),
        ({**host, "Host": f"127.0.0.1:{port + 1}"}, "unknown Host"),
        ({**host, "Origin": "null"}, "cross-origin request"),
        ({**host, "Origin": f"http://localhost:{port + 1}"}, "cross-origin request"),
        ({**host, "Origin": f"https://127.0.0.1:{port}"}, "cross-origin request"),
        ({"Host": f"127.0.0.1:{port}"}, "missing or wrong token"),
        ({**host, "X-Togeari-Token": "wrong"}, "missing or wrong token"),
        ({"Host": f"127.0.0.1:{port}", "Authorization": "Basic s3cret"}, "missing or wrong token"),
    ]
    for headers, reason in cases:
        status, body = get(port, "/queries/knobs", headers)
        assert (status, body) == (403, {"error": reason}), headers


def test_every_token_form_is_accepted(served):
    port = served
    host = {"Host": f"localhost:{port}"}
    for headers, path in [
        ({**host, "X-Togeari-Token": "s3cret"}, "/queries/knobs"),
        ({**host, "Authorization": "Bearer s3cret"}, "/queries/knobs"),
        (host, "/queries/knobs?token=s3cret"),
        ({**host, "Origin": f"http://localhost:{port}", "X-Togeari-Token": "s3cret"}, "/"),
    ]:
        status, body = get(port, path, headers)
        assert status == 200, (headers, path, body)


def test_unknown_names_and_routes_name_themselves(served):
    port = served
    ok = {"Host": f"127.0.0.1:{port}", "X-Togeari-Token": "s3cret"}
    assert get(port, "/queries/nope", ok) == (404, {"error": "no such name 'nope'"})
    assert get(port, "/elsewhere", ok) == (404, {"error": "no route /elsewhere"})
