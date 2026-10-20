"""Engine behaviour checks B1-B7 (spec/07 §7.3), run against any engine.

``check_engine(factory, sample_items)`` takes the engine from ``factory()``
(started or not), calls its ``start()`` on the calling thread, then drives it
from a separate driver thread the way a dispatcher does (``submit``,
``cancel``, ``capacity``, ``load`` from that one thread only) and returns an
``EngineReport`` with one ``RuleResult`` per rule.

``sample_items`` are templates: every submit uses a fresh ``WorkItem`` built
from one. Samples must succeed; ``failing_items`` must fail with
``ItemError``; ``kill()``, when given, kills the backend the way a crash does
(terminates the worker or device process, closes the service). The check then
waits up to ``kill_wait_s`` for ``ready()`` False or ``capacity()`` 0, and the
next submit must give ``UnitError``.

B3 instruments the engine: every ``submit``, ``cancel``, ``capacity``,
``load``, ``key`` and ``wake_at`` call is recorded with its thread, and a
heavy adapter's ``prepare`` (``light`` False) with its thread. A probe holds
``done`` open on the engine's thread until ``capacity()``, ``load()``,
``submit()`` and ``cancel()`` made by the driver after ``done`` started have
returned.
"""

import itertools
import math
import random
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError
from togeari.core.contracts.workitem import WorkItem

RULES = ("B1", "B2", "B3", "B4", "B5", "B6", "B7")
# Engine methods the machine calls from the dispatcher thread only.
DRIVER_CALLS = ("submit", "cancel", "capacity", "load", "key", "wake_at")
_RESULTS = (Ok, ItemError, UnitError, Cancelled)
_SHOW = 5
_PROBES = 3


@dataclass
class RuleResult:
    """One rule's verdict; a rule that does not apply is ok, its detail starting with "n/a"."""

    rule: str
    ok: bool
    detail: str


@dataclass
class EngineReport:
    """Every rule's verdict for one engine, in rule order."""

    engine: str
    rules: list[RuleResult]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rules)

    def rule(self, name: str) -> RuleResult:
        return next(r for r in self.rules if r.rule == name)

    def failed(self) -> list[str]:
        return [r.rule for r in self.rules if not r.ok]

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise AssertionError(str(self))

    def __str__(self) -> str:
        lines = [f"engine {self.engine}: {'PASS' if self.ok else 'FAIL'}"]
        for r in self.rules:
            lines.append(f"  {r.rule} {'pass' if r.ok else 'FAIL'}")
            lines.extend(f"      {line}" for line in r.detail.splitlines())
        return "\n".join(lines)


@dataclass
class _Verdict:
    """A rule's verdict while the checks run: None when the rule does not apply."""

    rule: str
    passed: bool | None
    details: list[str] = field(default_factory=list)

    def public(self) -> RuleResult:
        detail = "\n".join(self.details)
        if self.passed is None:
            detail = f"n/a: {detail}" if detail else "n/a"
        return RuleResult(self.rule, self.passed is not False, detail)


def same(a: Any, b: Any, tolerance: Any = None) -> bool:
    """Equal outputs: exact, within a relative ``tolerance``, or by ``tolerance(a, b)``."""
    if callable(tolerance):
        return bool(tolerance(a, b))
    tol = float(tolerance or 0.0)
    match a:
        case dict():
            return (
                isinstance(b, dict)
                and a.keys() == b.keys()
                and all(same(a[k], b[k], tol) for k in a)
            )
        case list() | tuple():
            return (
                isinstance(b, (list, tuple))
                and len(a) == len(b)
                and all(same(x, y, tol) for x, y in zip(a, b))
            )
        case bool():
            return a == b
        case int() | float():
            return isinstance(b, (int, float)) and abs(a - b) <= tol * max(1.0, abs(b))
        case _ if hasattr(a, "tolist") and hasattr(b, "tolist"):
            return same(a.tolist(), b.tolist(), tol)
        case _:
            return bool(a == b)


class _Watched:
    """A heavy adapter whose ``prepare`` calls on the driver thread are counted."""

    def __init__(self, inner: Any, driver: "_Driver"):
        self._inner = inner
        self._driver = driver

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def prepare(self, inputs: dict, protocol: str) -> Any:
        if threading.get_ident() == self._driver.thread_id:
            self._driver.heavy_on_driver += 1
        return self._inner.prepare(inputs, protocol)


class _Driver:
    """Makes every engine call from one thread and records every ``done``."""

    def __init__(
        self,
        engine: Any,
        samples: list,
        failing: list,
        tolerance: Any,
        timeout_s: float,
        seed: int,
        probe_s: float,
    ):
        self.engine = engine
        self.samples = samples
        self.failing = failing
        self.tolerance = tolerance
        self.timeout_s = timeout_s
        self.probe_s = probe_s
        self.rng = random.Random(seed)
        self.batch = getattr(engine, "kind", "call") == "batch"
        self.max_batch = max(1, int(getattr(engine, "max_batch", 1))) if self.batch else 1
        self.protocol = getattr(engine, "protocol", "python")

        # Guarded by ``cv``: answers per item id, done threads, malformed calls.
        self.cv = threading.Condition()
        self.answers: dict[int, list] = defaultdict(list)
        self.done_threads: set[int] = set()
        self.malformed: list[str] = []
        self.inline_ok = 0
        self.ids = itertools.count()
        self.items: dict[int, WorkItem] = {}
        self.origin: dict[int, int] = {}

        # Driver-side records: outputs of each sample alone, every item id submitted.
        self.reference: dict[int, Any] = {}
        self.every: list[int] = []
        self.thread_id = 0
        self.local = threading.local()
        self.submit_s: list[float] = []
        self.call_errors: list[str] = []

        # B3 instrumentation: (method, thread name) of calls off the driver thread,
        # threads of every engine call, heavy prepares on the driver, probe findings.
        self.off_driver: set[tuple[str, str]] = set()
        self.call_threads: set[int] = set()
        self.heavy_on_driver = 0
        self.probe_notes: list[str] = []
        self.wrapped: dict[str, Any] = {}
        self.instrumented: list[str] = []

    # ------------------------------------------------------------ instrumentation
    def instrument(self) -> None:
        """Wrap the engine's dispatcher-only methods to record the calling thread."""
        own = getattr(self.engine, "__dict__", {})
        for name in DRIVER_CALLS:
            method = getattr(self.engine, name, None)
            if method is None:
                continue
            before = own.get(name)
            try:
                setattr(self.engine, name, self._recording(name, method))
            except (AttributeError, TypeError):
                continue
            self.wrapped[name] = before
            self.instrumented.append(name)

    def restore(self) -> None:
        """Put the engine's own methods back."""
        for name, before in self.wrapped.items():
            if before is not None:
                setattr(self.engine, name, before)
            else:
                delattr(self.engine, name)
        self.wrapped.clear()

    def _recording(self, name: str, method: Callable) -> Callable:
        def call(*args: Any, **kwargs: Any) -> Any:
            ident = threading.get_ident()
            self.call_threads.add(ident)
            if ident != self.thread_id:
                self.off_driver.add((name, threading.current_thread().name))
            return method(*args, **kwargs)

        return call

    # ------------------------------------------------------------ items and calls
    def fresh(self, index: int) -> WorkItem:
        t = self.samples[index] if index >= 0 else self.failing[-index - 1]
        adapter = t.adapter if getattr(t.adapter, "light", False) else _Watched(t.adapter, self)
        item = WorkItem(
            t.node, dict(t.inputs), adapter, protocol=self.protocol, priority=t.priority
        )
        iid = next(self.ids)
        item.ext["check_id"] = iid
        self.items[iid] = item
        self.origin[iid] = index
        return item

    def call(self, name: str, *args: Any) -> Any:
        try:
            return getattr(self.engine, name)(*args)
        except Exception as exc:  # noqa: BLE001
            self.call_errors.append(f"{name} raised {exc!r}")
            return None

    def submit(self, items: list) -> tuple[Any, list[int]]:
        ids = [it.ext["check_id"] for it in items]
        work = items if self.batch else items[0]
        self.local.in_submit = True
        t0 = time.perf_counter()
        ticket = self.call("submit", work, self._done(ids))
        self.submit_s.append(time.perf_counter() - t0)
        self.local.in_submit = False
        return ticket, ids

    def _done(self, ids: list[int]) -> Callable:
        def done(ticket: Any, results: Any) -> None:
            inline = getattr(self.local, "in_submit", False)
            with self.cv:
                self.done_threads.add(threading.get_ident())
                if not isinstance(results, list) or len(results) != len(ids):
                    self.malformed.append(f"done with {results!r} for {len(ids)} items")
                else:
                    for iid, r in zip(ids, results):
                        if not isinstance(r, _RESULTS):
                            self.malformed.append(f"result {r!r} is not a Result")
                        if inline and isinstance(r, Ok):
                            self.inline_ok += 1
                        self.answers[iid].append(r)
                self.cv.notify_all()

        return done

    def wait(self, ids: list[int], timeout_s: float | None = None) -> bool:
        with self.cv:
            return self.cv.wait_for(
                lambda: all(self.answers[i] for i in ids), timeout_s or self.timeout_s
            )

    def wait_capacity(self) -> bool:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if (self.call("capacity") or 0) > 0:
                return True
            time.sleep(0.0005)
        return False

    def value(self, iid: int, r: Any) -> Any:
        return r.value if r.finished else self.items[iid].finish(r.value)

    def groups(self) -> list[list[int]]:
        """Sample indices that may share a batch (equal ``key``)."""
        if not self.batch:
            return [list(range(len(self.samples)))]
        by_key: dict[Any, list[int]] = defaultdict(list)
        for i in range(len(self.samples)):
            by_key[repr(self.call("key", self.fresh(i)))].append(i)
        return list(by_key.values())

    def draw(self, groups: list[list[int]]) -> list[WorkItem]:
        group = self.rng.choice(groups)
        n = self.rng.randint(1, self.max_batch) if self.batch else 1
        return [self.fresh(self.rng.choice(group)) for _ in range(n)]


def check_engine(
    factory: Callable[[], Any],
    sample_items: list,
    failing_items: list | tuple = (),
    kill: Callable[[], None] | None = None,
    tolerance: float | Callable[[Any, Any], bool] | None = None,
    *,
    submit_bound_ms: float = 2.0,
    submit_quantile: float = 0.99,
    submit_max_ms: float = 50.0,
    rounds: int = 200,
    cancel_rate: float = 0.25,
    timeout_s: float = 30.0,
    grace_s: float = 0.2,
    seed: int = 0,
    kill_wait_s: float = 2.0,
    probe_s: float = 1.0,
) -> EngineReport:
    """Run B1-B7 against ``factory()``; see the module docstring."""
    if not sample_items:
        raise ValueError("check_engine needs at least one sample item")
    engine = factory()
    engine.start()
    deadline = time.monotonic() + timeout_s
    while not engine.ready():
        if time.monotonic() > deadline:
            engine.stop()
            raise TimeoutError(f"engine {engine.name!r} not ready after {timeout_s}s")
        time.sleep(0.01)

    d = _Driver(
        engine, list(sample_items), list(failing_items), tolerance, timeout_s, seed, probe_s
    )
    rules: dict[str, _Verdict] = {}
    crashed: list[BaseException] = []

    def drive() -> None:
        d.thread_id = threading.get_ident()
        try:
            limit = _SubmitBound(submit_bound_ms / 1000.0, submit_quantile, submit_max_ms / 1000.0)
            _run(d, rules, kill, kill_wait_s, limit, rounds, cancel_rate)
            time.sleep(grace_s)
            _exactly_once(d, rules)
        except BaseException as exc:  # noqa: BLE001
            crashed.append(exc)

    d.instrument()
    driver = threading.Thread(target=drive, name="check-driver", daemon=True)
    driver.start()
    driver.join()
    d.restore()
    try:
        engine.stop()
    except Exception as exc:  # noqa: BLE001
        d.call_errors.append(f"stop raised {exc!r}")
    if crashed:
        raise crashed[0]
    _b3(d, rules)
    name = getattr(engine, "name", type(engine).__name__)
    return EngineReport(name, [rules[r].public() for r in RULES])


@dataclass
class _SubmitBound:
    """B1 limits: the ``quantile`` of submit times within ``bound_s``, every one within
    ``max_s``.
    """

    bound_s: float
    quantile: float
    max_s: float


@dataclass
class _Rounds:
    """Item ids of the B2 / B5 rounds: every one, those never cancelled, those cancelled."""

    every: list[int] = field(default_factory=list)
    plain: list[int] = field(default_factory=list)
    cancelled: set[int] = field(default_factory=set)


def _run(
    d: _Driver,
    rules: dict,
    kill: Any,
    kill_wait_s: float,
    limit: _SubmitBound,
    rounds: int,
    cancel_rate: float,
) -> None:
    """Every rule in turn; each step adds its verdict to ``rules``."""
    groups = d.groups()
    alone_failures = _reference(d)
    done = _rounds(d, groups, rounds, cancel_rate)
    _b2(d, rules, done, alone_failures, rounds)
    _b5(d, rules, done.cancelled)
    over = _b4(d, rules, groups)
    _b6(d, rules, groups, done.plain + over)

    # B3 probe: engine calls return while done runs on an engine thread.
    for i in range(min(_PROBES, len(d.samples))):
        _probe(d, i)

    _b7(d, rules, kill, kill_wait_s)
    _b1(d, rules, limit)


def _reference(d: _Driver) -> list[str]:
    """Every sample alone: its output goes to ``d.reference``; returns the failures."""
    failures = []
    for i in range(len(d.samples)):
        d.wait_capacity()
        _, ids = d.submit([d.fresh(i)])
        if not d.wait(ids):
            failures.append(f"sample {i}: no answer alone")
            continue
        r = d.answers[ids[0]][0]
        if isinstance(r, Ok):
            d.reference[i] = d.value(ids[0], r)
        else:
            failures.append(f"sample {i} alone gave {r!r}")
    return failures


def _rounds(d: _Driver, groups: list, rounds: int, cancel_rate: float) -> _Rounds:
    """Many submits within capacity, with random cancels now or a little later."""
    done = _Rounds()
    later: list[tuple[float, Any, list[int]]] = []
    for n in range(rounds):
        if not d.wait_capacity():
            break
        ticket, ids = d.submit(d.draw(groups))
        done.every.extend(ids)

        # Cancel now, cancel within 5 ms, or leave alone
        roll = d.rng.random()
        if roll < cancel_rate / 2:
            d.call("cancel", ticket)
            done.cancelled.update(ids)
        elif roll < cancel_rate:
            later.append((time.monotonic() + d.rng.random() * 0.005, ticket, ids))
        else:
            done.plain.extend(ids)

        # Late cancels that came due
        now = time.monotonic()
        for entry in [e for e in later if e[0] <= now]:
            later.remove(entry)
            d.call("cancel", entry[1])
            done.cancelled.update(entry[2])
        if n % 16 == 0:
            d.call("load")

    for _, ticket, ids in later:
        d.call("cancel", ticket)
        done.cancelled.update(ids)
    return done


def _b2(d: _Driver, rules: dict, done: _Rounds, alone_failures: list, rounds: int) -> None:
    """B2: every item of the rounds answered; every uncancelled one Ok."""
    answered = d.wait(done.every)
    d.every.extend(done.every)
    b2 = list(alone_failures)
    if not answered:
        lost = sum(1 for i in done.every if not d.answers[i])
        b2.append(f"{lost} of {len(done.every)} items unanswered")
    wrong = [i for i in done.plain if d.answers[i] and not isinstance(d.answers[i][0], Ok)]
    if wrong:
        b2.append(f"{len(wrong)} uncancelled items not Ok, e.g. {d.answers[wrong[0]][0]!r}")
    rules["B2"] = _Verdict("B2", not b2, [f"{len(done.every)} items in {rounds} submits", *b2])


def _b5(d: _Driver, rules: dict, cancelled: set[int]) -> None:
    """B5: a cancelled item answers Cancelled, or its normal result equal to the reference."""
    b5 = []
    for i in cancelled:
        if not d.answers[i]:
            continue
        r = d.answers[i][0]
        match r:
            case Cancelled():
                pass
            case Ok() if d.origin[i] in d.reference:
                if not same(d.value(i, r), d.reference[d.origin[i]], d.tolerance):
                    b5.append(f"item {i}: normal result after cancel differs from reference")
            case _:
                b5.append(f"item {i}: {r!r} after cancel")
    kinds = sum(isinstance(d.answers[i][0], Cancelled) for i in cancelled if d.answers[i])
    rules["B5"] = _Verdict(
        "B5", not b5, [f"{len(cancelled)} cancelled items, {kinds} answered Cancelled", *b5[:_SHOW]]
    )


def _b4(d: _Driver, rules: dict, groups: list) -> list[int]:
    """B4: at capacity and one past it, every item is answered; returns the item ids."""
    d.wait_capacity()
    cap = d.call("capacity") or 0
    over: list[int] = []
    for _ in range(cap + 1):
        _, ids = d.submit(d.draw(groups))
        over.extend(ids)
    ok4 = d.wait(over)
    lost = sum(1 for i in over if not d.answers[i])
    not_ok = [d.answers[i][0] for i in over if d.answers[i] and not isinstance(d.answers[i][0], Ok)]
    b4 = []
    if cap <= 0:
        b4.append("capacity() was 0 on an idle engine")
    if not ok4:
        b4.append(f"{lost} of {len(over)} items unanswered")
    if not_ok:
        b4.append(f"{len(not_ok)} items not Ok, e.g. {not_ok[0]!r}")
    rules["B4"] = _Verdict(
        "B4", not b4, [f"capacity {cap}, submitted {cap + 1} at once ({len(over)} items)", *b4]
    )
    d.every.extend(over)
    return over


def _b6(d: _Driver, rules: dict, groups: list, seen: list[int]) -> None:
    """B6: outputs inside mixed batches equal the outputs alone (B2 for other engines)."""
    reference = d.reference
    mixed = [i for i in seen if d.answers[i] and isinstance(d.answers[i][0], Ok)]

    # Batch engines: every group again, shuffled into full batches
    if d.batch:
        for group in groups:
            pool = group * max(1, d.max_batch // max(1, len(group)) + 1)
            d.rng.shuffle(pool)
            for start in range(0, len(pool), d.max_batch):
                d.wait_capacity()
                _, ids = d.submit([d.fresh(i) for i in pool[start : start + d.max_batch]])
                d.wait(ids)
                d.every.extend(ids)
                mixed.extend(i for i in ids if d.answers[i] and isinstance(d.answers[i][0], Ok))
    differ = [
        i
        for i in mixed
        if d.origin[i] in reference
        and not same(d.value(i, d.answers[i][0]), reference[d.origin[i]], d.tolerance)
    ]
    b6 = [f"{len(mixed)} outputs compared with their sample alone"]
    b6 += [f"item {i} (sample {d.origin[i]}) differs from its output alone" for i in differ[:_SHOW]]
    if d.batch:
        rules["B6"] = _Verdict("B6", not differ, b6)
    else:
        rules["B6"] = _Verdict("B6", None, ["not a batch engine", *b6[1:]])
        if differ:
            rules["B2"].passed = False
            rules["B2"].details.extend(b6[1:])


def _b7(d: _Driver, rules: dict, kill: Any, kill_wait_s: float) -> None:
    """B7: failing items give ItemError; a killed backend gives UnitError."""
    b7: list[str] = []
    for k in range(len(d.failing)):
        d.wait_capacity()
        _, ids = d.submit([d.fresh(-k - 1)])
        d.every.extend(ids)
        if not d.wait(ids):
            b7.append(f"failing item {k}: no answer")
        elif not isinstance(d.answers[ids[0]][0], ItemError):
            b7.append(f"failing item {k}: {d.answers[ids[0]][0]!r}, expected ItemError")

    # The killed backend: down within kill_wait_s, then a submit answers UnitError
    if kill is not None:
        kill()
        deadline = time.monotonic() + kill_wait_s
        while d.engine.ready() and (d.call("capacity") or 0) > 0:
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)
        _, ids = d.submit([d.fresh(0)])
        d.every.extend(ids)
        if not d.wait(ids):
            b7.append("after kill: no answer")
        elif not isinstance(d.answers[ids[0]][0], UnitError):
            b7.append(f"after kill: {d.answers[ids[0]][0]!r}, expected UnitError")
    notes = [f"{len(d.failing)} failing items", "kill checked" if kill else "no kill given"]
    applies = bool(d.failing) or kill is not None
    rules["B7"] = _Verdict("B7", (not b7) if applies else None, notes + b7)


def _b1(d: _Driver, rules: dict, limit: _SubmitBound) -> None:
    """B1: submit times within their limits; no Ok result is produced inside submit."""
    times = sorted(d.submit_s)
    at = times[min(len(times) - 1, max(0, int(round(limit.quantile * len(times))) - 1))]
    b1 = [
        f"{len(times)} submits: p50 {times[len(times) // 2] * 1e3:.3f} ms, "
        f"q{limit.quantile:g} {at * 1e3:.3f} ms (bound {limit.bound_s * 1e3:g} ms), "
        f"max {times[-1] * 1e3:.3f} ms (bound {limit.max_s * 1e3:g} ms)"
    ]

    # The quantile bound applies once there are enough submits to tell it from the max
    needed = math.ceil(1.0 / (1.0 - limit.quantile)) if limit.quantile < 1.0 else 1
    quantile_ok = at <= limit.bound_s or len(times) < needed
    if len(times) < needed:
        b1.append(f"q{limit.quantile:g} needs {needed} submits; only the max bound applies")
    elif at > limit.bound_s:
        b1.append(f"the q{limit.quantile:g} submit time exceeded its bound")
    if times[-1] > limit.max_s:
        b1.append("a submit exceeded the max bound")
    b1.append(f"{d.inline_ok} Ok results delivered inside submit (must be 0)")
    within = quantile_ok and times[-1] <= limit.max_s
    rules["B1"] = _Verdict("B1", within and not d.inline_ok, b1)


def _exactly_once(d: _Driver, rules: dict) -> None:
    """B2 / B5: after a grace period, every item has exactly one result."""
    with d.cv:
        counts = {i: len(d.answers[i]) for i in d.every}
    dup = [i for i, c in counts.items() if c > 1]
    missing = [i for i, c in counts.items() if c == 0]
    for rule in ("B2", "B5"):
        if dup:
            rules[rule].passed = False
            rules[rule].details.append(f"{len(dup)} items answered more than once, e.g. {dup[0]}")
    if missing:
        rules["B2"].passed = False
        rules["B2"].details.append(f"{len(missing)} items never answered")


def _probe(d: _Driver, index: int) -> None:
    """Hold ``done`` on the engine's thread until a driver call started after it returns."""
    entered = threading.Event()
    ack = threading.Event()
    finished = threading.Event()
    item = d.fresh(index)
    ids = [item.ext["check_id"]]
    inner = d._done(ids)

    def done(ticket: Any, results: Any) -> None:
        if threading.get_ident() != d.thread_id:
            entered.set()
            if not ack.wait(d.probe_s):
                d.probe_notes.append(
                    f"engine calls made while done ran did not return in {d.probe_s}s: "
                    "done is called under a lock those calls take"
                )
        inner(ticket, results)
        finished.set()

    d.wait_capacity()
    d.call("submit", item if not d.batch else [item], done)
    d.every.extend(ids)
    deadline = time.monotonic() + d.timeout_s
    while not entered.wait(0.001) and not finished.is_set():
        if time.monotonic() > deadline:
            d.probe_notes.append(f"probe {index}: no answer")
            return
    if entered.is_set():
        d.call("capacity")
        d.call("load")
        ticket, more = d.submit([d.fresh(index)])
        d.call("cancel", ticket)
        d.every.extend(more)
        ack.set()
    if not finished.wait(d.timeout_s):
        d.probe_notes.append(f"probe {index}: done did not finish")


def _b3(d: _Driver, rules: dict) -> None:
    """B3: engine calls on the driver thread only; done safe on engine threads; heavy prepares
    off.
    """
    details = [
        f"engine calls from {len(d.call_threads)} thread(s) ({', '.join(d.instrumented) or 'none'} "
        f"instrumented); done from {len(d.done_threads)} thread(s), "
        f"{len(d.done_threads - {d.thread_id})} of them engine threads"
    ]
    details += [f"{name} called from thread {who!r}" for name, who in sorted(d.off_driver)][:_SHOW]
    if d.heavy_on_driver:
        details.append(f"{d.heavy_on_driver} heavy-adapter prepare calls on the driver thread")
    details += d.probe_notes[:_SHOW] + d.call_errors[:_SHOW] + d.malformed[:_SHOW]
    ok = not (d.off_driver or d.heavy_on_driver or d.probe_notes or d.call_errors or d.malformed)
    rules["B3"] = _Verdict("B3", ok, details)
