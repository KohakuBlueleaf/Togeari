"""Record telemetry runs of a captioning-pipeline machine for the dashboard's dev server.

Each scenario runs the same program on sim engines for ``--seconds`` and writes its telemetry
to ``<out>/<scenario>/``. The VLM lane is a stream lane: each unit runs 16 requests at once
and keeps the rest waiting inside itself, and admission follows that waiting count
(``TargetLoad``), as a serving engine and GBC's machine do.

- ``steady``: every lane keeps up;
- ``saturated``: one VLM unit, so requests wait in the engine and admission holds back;
- ``flaky``: the tagger fails 5 % of its items and one VLM unit is down for a while.

``--live`` runs one machine (``<out>/live``) until interrupted, for the dashboard's live side:
the inspector loaded (its token in ``machine.json``), VLM unit 1 down 10 s of every 40 s, and
contributed policies to swap from the dashboard (admission, the detector's former, selectors).

Usage: ``python record_runs.py [out=dev-runs] [--seconds 90] [--scenarios a,b] [--core rust]
[--live]``.
"""

import argparse
import shutil
import time
from pathlib import Path

from kohakunode import NodeLibrary

from togeari import Inline, Lane, Machine, Use
from togeari.core.plugin.base import BasePlugin
from togeari.io import Iterable, Memory
from togeari.plugins import Counters, Inspector
from togeari.plugins.telemetry import Telemetry
from togeari.policies import LeastLoaded, RoundRobin, SizeOrWait, TargetLoad, Throughput
from togeari.testing import SimBatch, SimCall, SimStream

# A live machine's VLM unit 1 is down this long of every period (seconds), for a day
OUTAGE_EVERY_S, OUTAGE_S = 40.0, 10.0

lib = NodeLibrary("pipeline")


@lib.node()
def load(url: int) -> int:
    return url * 7 + 1


@lib.node()
def detect(image: int) -> list:
    return list(range(1 + image % 5))


@lib.node()
def crop(image: int, box: int) -> int:
    return image * 10 + box


@lib.node()
def caption(region: int) -> str:
    return f"region {region}"


@lib.node()
def tag(image: int) -> list:
    return [image % 3, image % 7]


@lib.node()
def merge(captions: list, tags: list) -> dict:
    return {"captions": captions, "tags": tags}


@lib.node()
def score(record: dict) -> float:
    return len(record["captions"]) / 5.0


@lib.node()
def keep(score: float) -> bool:
    return score > 0.3


@lib.node()
def write(record: dict, score: float) -> int:
    return len(record["captions"])


PROGRAM = """
@def (url: int)main(done):
    (url)load(image)
    (image)detect(boxes)
    (image)tag(tags)
    (boxes)collect(box, `one`, text -> captions)
    one:
        (image, box)crop(region)
        (region)caption(text)
    (captions, tags)merge(record)
    (record)score(s)
    (s)keep(ok)
    (ok)branch(`yes`, `no`)
    yes:
        (record, s)write(done)
    no:
        done = 0
"""


class DevPolicies(BasePlugin):
    """Policies the dashboard can name: admission rules on the VLM lane, the detector's
    formers, unit selectors."""

    name = "dev-policies"

    def __init__(self, vlm: Lane):
        self.vlm = vlm

    def contribute_policies(self) -> dict:
        return {
            "admit-tight": TargetLoad(self.vlm, waiting=16, max_jobs=128),
            "admit-loose": TargetLoad(self.vlm, waiting=96, max_jobs=768),
            "detect-small": SizeOrWait(4, 2),
            "detect-large": SizeOrWait(32, 8),
            "least-loaded": LeastLoaded(),
            "round-robin": RoundRobin(),
            "throughput": Throughput(),
        }


def machine(scenario: str, out: Path, seconds: float, core: str) -> Machine:
    # The VLM is a stream engine: 16 requests run at once, the rest wait inside it
    def vlm_unit(i: int, outages: list | None = None) -> SimStream:
        return SimStream(
            name=f"vlm{i}", latency_ms=30, tail=0.3, slots=16, seed=1 + i, outages=outages
        )

    if scenario == "saturated":
        vlm_units = [vlm_unit(0)]
    elif scenario == "live":
        periods = range(int(86400 / OUTAGE_EVERY_S))
        down = [(k * OUTAGE_EVERY_S + 20.0, k * OUTAGE_EVERY_S + 20.0 + OUTAGE_S) for k in periods]
        vlm_units = [vlm_unit(0), vlm_unit(1, down)]
    else:
        down = [(seconds * 0.35, seconds * 0.55)] if scenario == "flaky" else None
        vlm_units = [vlm_unit(0), vlm_unit(1, down)]
    vlm = Lane(vlm_units, name="vlm")
    io = Lane(
        [SimCall(name=f"io{i}", latency_ms=4, capacity=32, seed=10 + i) for i in range(2)],
        name="io",
    )
    gpu = Lane(
        [SimBatch(name=f"det{i}", latency_ms=3, max_batch=16, seed=20 + i) for i in range(2)],
        former=SizeOrWait(16, 4),
        name="detect",
    )
    tagger = Lane(
        [
            SimCall(
                name="tagger0",
                latency_ms=12,
                capacity=24,
                seed=30,
                item_error_rate=0.05 if scenario == "flaky" else 0.0,
            )
        ],
        name="tagger",
    )
    plugins = [Telemetry(out, trace_rate=0.01, inspector_token=scenario == "live"), Counters()]
    if scenario == "live":
        plugins += [Inspector(), DevPolicies(vlm)]
    return Machine(
        program=PROGRAM,
        entry="main",
        libraries=[lib],
        bind={
            "load": Use(io),
            "write": Use(io),
            "detect": Use(gpu),
            "caption": Use(vlm),
            "tag": Use(tagger),
            "crop": Inline(),
            "merge": Inline(),
            "score": Inline(),
            "keep": Inline(),
            "kir.std.*": Inline(),
        },
        admission=TargetLoad(vlm, waiting=48, max_jobs=384),
        plugins=plugins,
        core=core,
    )


def jobs(seconds: float | None):
    """Jobs until ``seconds`` have passed (None: forever), about 120 a second."""
    end = None if seconds is None else time.monotonic() + seconds
    i = 0
    while end is None or time.monotonic() < end:
        yield {"url": i}
        i += 1
        if i % 50 == 0:
            time.sleep(0.4)


def main() -> None:
    p = argparse.ArgumentParser(description="Record telemetry runs for the dashboard")
    p.add_argument("out", nargs="?", default="dev-runs")
    p.add_argument("--seconds", type=float, default=90.0)
    p.add_argument("--scenarios", default="steady,saturated,flaky")
    p.add_argument("--core", default="rust")
    p.add_argument("--live", action="store_true", help="one machine until interrupted")
    a = p.parse_args()
    if a.live:
        out = Path(a.out) / "live"
        shutil.rmtree(out, ignore_errors=True)
        m = machine("live", out, 0.0, a.core)
        with m:
            inspector = next(p for p in m.plugins.plugins if p.name == "inspector")
            print(f"live: {out}, inspector {inspector.url}", flush=True)
            try:
                m.run(Iterable(jobs(None)), Memory())
            except KeyboardInterrupt:
                print("live: stopping", flush=True)
        return
    for scenario in a.scenarios.split(","):
        out = Path(a.out) / scenario
        shutil.rmtree(out, ignore_errors=True)
        m = machine(scenario, out, a.seconds, a.core)
        with m:
            summary = m.run(Iterable(jobs(a.seconds)), Memory())
            time.sleep(2.5)
        print(f"{scenario}: {summary['jobs']} jobs in {summary['seconds']:.1f}s -> {out}")


if __name__ == "__main__":
    main()
