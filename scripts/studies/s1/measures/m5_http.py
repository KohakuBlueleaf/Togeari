"""M5: HTTP client cost of ``WebCallEngine`` against the local echo service.

One config per target rate (the engine's token bucket; ``max`` is unlimited).
Each request is a JSON body of ``m5_pad_bytes`` padding, echoed back. Phases
feed the engine directly or through the machine, alone and next to the chain
load. Reports requests/s, latency, this process's CPU per request (encode,
send, receive, decode, all client threads), the service's CPU per request,
and the dispatcher's calls/s and busy fraction.
"""

import time

import httpx

from s1.common import dispatch, phases
from s1.common.feeders import JobFeeder, UnitFeeder
from s1.scales import Scale
from s1.workloads import http_echo
from togeari import Use
from togeari.adapters.http import JsonHttp
from togeari.core.contracts.workitem import WorkItem
from togeari.engines.web import WebCallEngine

NAME = "m5"
TITLE = "HTTP client cost"


def plan(scale: Scale) -> list[dict]:
    return [
        {"name": f"rate-{int(r)}" if r is not None else "rate-max", "rate": r}
        for r in scale.m5_rates
    ]


def echoed(body: dict) -> int:
    return body["x"]


def per_request(rows: list[dict]) -> None:
    """Add client and service CPU per completed request (µs) to each phase without chain load."""
    for row in rows:
        feeder = row.get("feeders", {}).get("work")
        if feeder is None or row.get("cpu") is None or "chain" in row["feeders"]:
            continue
        ok = feeder["items_ok"]
        service = row["probes"]["service_cpu"]["delta"]
        row["client_cpu_us_per_request"] = row["cpu"]["process_cpu_s"] / ok * 1e6 if ok else None
        row["service_cpu_us_per_request"] = service / ok * 1e6 if ok else None


def run(cfg: dict, scale: Scale) -> dict:
    rate = cfg["rate"]
    pad = "p" * scale.m5_pad_bytes
    adapter = JsonHttp("/echo", parse=echoed)

    with http_echo.EchoService() as service, httpx.Client(base_url=service.url) as side:
        engine = WebCallEngine(
            service.url,
            max_inflight=scale.m5_inflight,
            rate=(rate, max(1.0, rate / 50)) if rate is not None else None,
            name="web",
        )

        # The machine starts the engine
        t0 = time.perf_counter()
        machine = dispatch.build(
            scale.chain_len,
            work=("call", ("x", "pad")),
            libraries=(http_echo.lib,),
            bind={"call": Use(engine, adapter=adapter)},
        )
        start_s = time.perf_counter() - t0

        def inputs(i: int) -> dict:
            return {"x": i, "pad": pad}

        def item(i: int) -> WorkItem:
            return WorkItem("call", inputs(i), adapter, "http")

        def service_cpu() -> float:
            return side.get("/cpu").json()["cpu"]

        work = {
            "direct": lambda: [UnitFeeder(engine, item, name="work")],
            "lane": lambda: [JobFeeder(machine, "work", inputs, scale.m5_inflight, name="work")],
        }
        try:
            rows = phases.run_all(
                scale.m5_phases,
                scale,
                work,
                machine,
                probes={"engine": lambda: engine.load().extra, "service_cpu": service_cpu},
            )
            spec = engine.spec()
        finally:
            machine.stop()
    per_request(rows)
    return {"start_s": start_s, "engine": spec, "phases": rows}
