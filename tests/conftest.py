"""Shared helpers for the machine checks: the generator's machine, outage units, bounded waits,
and the per-test watchdog."""

import faulthandler
import os
import random
import threading

import pytest
from kohakunode import STD
from kohakunode.testing import LIB

from togeari import Inline, Lane, Machine, SourceItem, Use
from togeari.conformance import conform
from togeari.io import Iterable, Memory
from togeari.policies import (
    Affinity,
    ByHint,
    Fixed,
    LeastLoaded,
    RoundRobin,
    SizeOrWait,
    Throughput,
)
from togeari.testing.sim import SimEngine

# Seconds a test may run before the watchdog dumps every thread and ends the session
TEST_TIMEOUT_S = float(os.environ.get("TOGEARI_TEST_TIMEOUT", "900"))

# Nodes of the generator's library and the std library with effects: replayed, never re-run
IMPURE = frozenset(
    name for lib in (LIB, STD) for name, sig in lib.signatures.items() if sig.ordered or sig.mutates
)

# Nodes a store may serve: the generator's nodes without effects
STORABLE = tuple(name for name in LIB.signatures if name not in IMPURE)

# Every builtin unit selector
SELECTORS = {
    "least_loaded": LeastLoaded,
    "throughput": Throughput,
    "round_robin": RoundRobin,
    "affinity": Affinity,
    "by_hint": ByHint,
}

# Unit capacities per engine kind
CAPACITY = {"call": 8, "batch": 2, "stream": 256}


def pure(name: str) -> bool:
    return name not in IMPURE


def pytest_configure(config):
    """The watchdog's log: ``runs/tests/watchdog.txt`` under the project root."""
    path = config.rootpath / "runs" / "tests" / "watchdog.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    config.watchdog_log = path.open("w", encoding="utf-8")


def pytest_unconfigure(config):
    log = getattr(config, "watchdog_log", None)
    if log is not None:
        log.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """Watchdog: a test past its bound writes every thread's stack to the watchdog log and
    exits the session (exit code 1)."""
    mark = item.get_closest_marker("timeout")
    seconds = mark.args[0] if mark else TEST_TIMEOUT_S
    log = item.config.watchdog_log
    log.write(f"{item.nodeid}: bound {seconds} s\n")
    log.flush()
    faulthandler.dump_traceback_later(seconds, exit=True, file=log)
    try:
        return (yield)
    finally:
        faulthandler.cancel_dump_traceback_later()


class CountOutage(SimEngine):
    """A sim unit down from its ``down_at``-th submit (or while ``down`` is set) until its
    ``ready()`` has been asked ``polls`` times."""

    def __init__(self, kind: str = "call", down_at: int | None = None, polls: int = 1, **kw):
        super().__init__(kind, **kw)
        self.down_at = down_at
        self.polls = polls
        self.down = False
        self.left = 0
        self.n_submits = 0

    def go_down(self, polls: int | None = None) -> None:
        self.down = True
        self.left = self.polls if polls is None else polls

    def in_outage(self) -> bool:
        return self.down

    def ready(self) -> bool:
        if self.down:
            self.left -= 1
            if self.left > 0:
                return False
            self.down = False
        return super().ready()

    def submit(self, work, done):
        self.n_submits += 1
        if self.n_submits == self.down_at:
            self.go_down()
        return super().submit(work, done)


def selector(name: str):
    return SELECTORS[name]()


def build(
    program=None,
    entry=None,
    n_units=1,
    seed=0,
    latency_ms=0.5,
    record=1.0,
    inline_std=True,
    programs=None,
    forming="late",
    select=None,
    store_mode=None,
    item_error_rate=0.0,
    outage=None,
    retry=None,
    **knobs,
):
    """A machine over the generator's library: every node on a sim lane of ``n_units`` units.

    ``select``: one selector name for every lane, or one per lane (call, batch, stream).
    ``store_mode``: ``content`` or ``positional`` on the nodes without effects.
    ``outage``: ``(down_at, polls)``: unit ``i`` of each lane but the last goes down at its
    ``down_at + i``-th submit and comes back after ``polls`` health polls.
    """
    rng = random.Random(seed)
    kinds = ("call", "batch", "stream")
    picks = [select] * 3 if select is None or isinstance(select, str) else list(select)

    def unit(kind: str, i: int):
        down = outage is not None and i < n_units - 1
        down_at, polls = (outage[0] + i, outage[1]) if down else (None, 1)
        return CountOutage(
            kind,
            down_at=down_at,
            polls=polls,
            name=f"{kind}{i}",
            latency_ms=latency_ms,
            seed=rng.random(),
            capacity=CAPACITY[kind],
            item_error_rate=item_error_rate,
        )

    lanes = {}
    for kind, pick in zip(kinds, picks, strict=True):
        lanes[kind] = Lane(
            [unit(kind, i) for i in range(n_units)],
            former=SizeOrWait(8, 1) if kind == "batch" else None,
            forming=forming,
            select=selector(pick) if pick is not None else None,
            retry=retry,
            name=kind,
        )

    store = knobs.pop("store", None)
    lane_of = {"inc": "call", "mix": "batch", "flaky": "stream", "small": "call"}
    lane_of |= {"total": "stream", "pick": "batch", "emit": "call", "bump": "call"}
    bind = {
        name: Use(lanes[k], store=store_mode if name in STORABLE else None)
        for name, k in lane_of.items()
    }
    bind["kir.std.*"] = Inline() if inline_std else Use(lanes["call"])
    bind["f*"] = Use(lanes["call"])
    return Machine(
        programs=programs if programs is not None else {program: [entry]},
        libraries=[LIB],
        bind=bind,
        admission=Fixed(32),
        record=record,
        seed=seed,
        store=store,
        **knobs,
    )


def run_jobs(machine, inputs):
    sink = Memory()
    machine.run(Iterable(inputs), sink)
    return sink


def keyed(inputs: list, prefix: str, entry: str | None = None) -> list[SourceItem]:
    """Jobs with explicit keys ``prefix + position``."""
    return [SourceItem(f"{prefix}{i}", dict(x), entry) for i, x in enumerate(inputs)]


def dump(machine) -> str:
    """The machine's resident jobs (with their detail for the first few) and lanes, for a
    failure message."""
    try:
        jobs = machine.inspect("jobs").result(5)
        detail = [machine.inspect("job", id=j["id"]).result(5) for j in jobs[:4]]
        lanes = machine.inspect("lanes").result(5)
    except Exception as exc:  # noqa: BLE001
        return f"(no answer from the dispatcher: {exc!r})"
    return f"jobs: {jobs}\ndetail: {detail}\nlanes: {lanes}"


def bounded(machine, fn, seconds: float):
    """``fn()`` on a daemon thread; past ``seconds`` the check fails with the machine's dump."""
    out: dict = {}

    def go():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        pytest.fail(f"no end after {seconds} s (hang)\n{dump(machine)}")
    if "error" in out:
        raise out["error"]
    return out.get("value")


def conform_all(machine, records, inputs_of, **kw) -> None:
    """Every record conforms against its own entry's job program."""
    for rec in records:
        jp = machine.code.entries[rec.entry].region.job
        v = conform(jp, rec, inputs_of[rec.key], **kw)
        assert v.ok, f"job {rec.key} ({rec.entry}): {v.reason}\n{v.reference}\n{v.machine}"
