"""Nodes of the parallel example: slow lookups that log when they run."""

import threading
import time

from kohakunode import NodeLibrary

lib = NodeLibrary("city")

# (job label, node, start, end) of every call, on the wall clock.
TIMELINE: list[tuple[str, str, float, float]] = []
_LOCK = threading.Lock()


def _work(label: str, node: str, seconds: float) -> None:
    start = time.time()
    time.sleep(seconds)
    with _LOCK:
        TIMELINE.append((label, node, start, time.time()))


@lib.node(outputs=["sky"])
def weather(city: str) -> str:
    _work(city, "weather", 0.30)
    return "storm" if len(city) % 2 else "sun"


@lib.node(outputs=["n_events"])
def events(city: str) -> int:
    _work(city, "events", 0.20)
    return len(city) * 3


@lib.node(outputs=["jam"])
def traffic(city: str) -> float:
    _work(city, "traffic", 0.25)
    return round(len(city) / 10, 2)


@lib.node(outputs=["storm"])
def is_stormy(sky: str) -> bool:
    return sky == "storm"


@lib.node(outputs=["note"])
def alert(city: str) -> str:
    _work(city, "alert", 0.15)
    return f"storm warning for {city}"


@lib.node(outputs=["score"])
def survey(city: str, district: str) -> int:
    _work(city, f"survey {district}", 0.10)
    return len(district) * 7 % 10


@lib.node(outputs=["summary"])
def compose(sky: str, n_events: int, jam: float, note: str, scores: list) -> str:
    return f"{sky}, {n_events} events, traffic {jam}, {note}, district scores {scores}"
