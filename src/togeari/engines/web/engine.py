"""``WebCallEngine``: HTTP requests from one asyncio I/O thread with pooled ``httpx`` clients."""

import asyncio
import contextlib
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from togeari.core.contracts.engine import EngineBase, Load
from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError
from togeari.engines.common.errors import EngineStopped
from togeari.engines.common.state import EngineState
from togeari.engines.common.ticket import Ticket, finish
from togeari.engines.web.classify import default_classify
from togeari.engines.web.limiter import TokenBucket

Classify = Callable[[httpx.Response | BaseException], ItemError | UnitError | None]

# What a unit error does to the other requests in flight
UNIT_ERROR_MODES = ("block", "cancel")


class _Pool:
    """One ``httpx`` client with ``size`` connections, and its requests in flight."""

    __slots__ = ("client", "size", "inflight")

    def __init__(self, client: httpx.AsyncClient, size: int):
        self.client = client
        self.size = size
        self.inflight = 0


def pool_sizes(max_inflight: int, pool_size: int) -> list[int]:
    """Connections per client: ``max_inflight`` split into pools of at most ``pool_size``."""
    n = max(1, math.ceil(max_inflight / max(1, pool_size)))
    return [max_inflight // n + (i < max_inflight % n) for i in range(n)]


class WebCallEngine(EngineBase):
    """A call engine for any HTTP service.

    ``submit`` hands the item to the I/O loop, which calls ``item.payload()``
    (an ``HttpRequest``), sends it, judges the outcome with ``classify`` and,
    when the outcome is not an error, answers ``item.finish(response)``.

    ``capacity()`` is the free part of ``max_inflight``, and 0 while the
    optional token bucket ``rate=(requests per second, burst)`` is empty;
    ``wake_at()`` is when the bucket next allows a request, or when a
    ``Retry-After`` block ends. ``load()`` reports requests in flight, a
    latency EWMA, error counts and the latest numbers from ``load_probe()``.
    A ``UnitError`` blocks the unit: ``ready()`` is False and ``capacity()`` is 0
    until its ``retry_after`` has passed and, with ``health_path`` set, until a
    background GET of that path succeeds. ``on_unit_error`` sets what happens to
    the other requests in flight: ``"block"`` (default) lets them finish and
    answer normally; ``"cancel"`` answers each with the same ``UnitError`` at once.
    A running request is never aborted (cancel or unit error): it runs to its end with
    its answer dropped, and its slot frees then.

    Connections are split over clients of at most ``pool_size`` connections
    each; a request goes to the client with the most free connections.
    """

    kind = "call"
    protocol = "http"
    shares_memory = False

    def __init__(
        self,
        base_url: str,
        max_inflight: int = 64,
        rate: tuple[float, float] | float | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float = 600.0,
        connect_timeout_s: float = 10.0,
        load_probe: Callable[[], dict] | None = None,
        classify: Classify | None = None,
        health_path: str | None = None,
        health_timeout_s: float = 2.0,
        probe_interval_s: float = 1.0,
        start_timeout_s: float = 10.0,
        pool_size: int = 2,
        on_unit_error: str = "block",
        max_retry_after_s: float = 3600.0,
        name: str | None = None,
    ):
        super().__init__(name or "web")
        if on_unit_error not in UNIT_ERROR_MODES:
            raise ValueError(f"on_unit_error {on_unit_error!r}: expected one of {UNIT_ERROR_MODES}")
        match rate:
            case None:
                self.bucket = None
            case (per_s, burst):
                self.bucket = TokenBucket(per_s, burst)
            case float() | int():
                self.bucket = TokenBucket(rate, 1.0)
            case _:
                raise ValueError(f"rate {rate!r}: expected (requests per second, burst)")
        if max_inflight < 1 or pool_size < 1:
            raise ValueError("max_inflight and pool_size must be at least 1")
        self.base_url = base_url
        self.max_inflight = max_inflight
        self.rate = rate
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self.connect_timeout_s = connect_timeout_s
        self.load_probe = load_probe
        self.classify = classify or default_classify
        self.health_path = health_path
        self.health_timeout_s = health_timeout_s
        self.probe_interval_s = probe_interval_s
        self.start_timeout_s = start_timeout_s
        self.pool_size = pool_size
        self.on_unit_error = on_unit_error
        self.max_retry_after_s = max_retry_after_s

        # Shared state, guarded by ``lock``.
        self.lock = threading.Lock()
        self.state = EngineState.STOPPED
        self.inflight = 0
        self.tickets: dict[int, Ticket] = {}
        # Tickets submitted but not yet past the token bucket.
        self.unpaced: set[int] = set()
        self.blocked_until = 0.0
        self.healthy = True
        self.health_pending = False
        self.latency = 0.0
        self.probed: dict = {}
        self.counts: dict[str, Any] = {
            "items": 0,
            "ok": 0,
            "cancelled": 0,
            "unit_errors": 0,
            "unit_error_items": 0,
        }
        self.errors: dict[str, int] = {}
        # Calls for the I/O loop, run together on one wake-up.
        self.inbox: list[tuple[Callable, Any]] = []
        self.kicked = False

        # Loop-side objects, created by ``start``.
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.pools: list[_Pool] = []
        # One entry per free connection, used as a stack.
        self.free: list[_Pool] = []
        # The health GET's own client, apart from the work connections
        self.health_client: httpx.AsyncClient | None = None
        self.closing: asyncio.Event | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the I/O loop; per-run state (requests in flight, queued calls) starts empty."""
        # One start at a time: a start while starting or up does nothing
        with self.lock:
            if self.state != EngineState.STOPPED:
                return
            self.state = EngineState.STARTING
            self.inflight = 0
            self.tickets.clear()
            self.unpaced.clear()
            self.inbox = []
            self.kicked = False
            self.health_pending = False
        loop = asyncio.new_event_loop()
        started = threading.Event()
        thread = threading.Thread(
            target=self._serve, args=(loop, started), name=f"{self.name}-io", daemon=True
        )
        self.loop, self.thread = loop, thread
        thread.start()

        # A loop that does not start in time is told to close, and the start fails
        if not started.wait(self.start_timeout_s):
            with self.lock:
                self.state = EngineState.STOPPED
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._close_now)
            thread.join(5.0)
            raise TimeoutError(f"{self.name}: I/O loop did not start")

        # Up only from STARTING: a stop during the start keeps the engine stopped
        with self.lock:
            if self.state != EngineState.STARTING:
                return
            self.state = EngineState.UP
            self.blocked_until = 0.0
            self.healthy = True

    def _close_now(self) -> None:
        if self.closing is not None:
            self.closing.set()

    def _serve(self, loop: asyncio.AbstractEventLoop, started: threading.Event) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main(started))
        finally:
            loop.close()

    async def _main(self, started: threading.Event) -> None:
        self.pools = [
            _Pool(
                httpx.AsyncClient(
                    base_url=self.base_url,
                    headers=self.headers,
                    # A connection that does not open in time is a unit error, not a long wait
                    timeout=httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s),
                    limits=httpx.Limits(max_connections=size, max_keepalive_connections=size),
                ),
                size,
            )
            for size in pool_sizes(self.max_inflight, self.pool_size)
        ]
        self.free = [pool for pool in self.pools for _ in range(pool.size)]
        if self.health_path is not None:
            self.health_client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers,
                timeout=httpx.Timeout(self.health_timeout_s),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            )
        self.closing = asyncio.Event()
        if self.load_probe is not None:
            asyncio.get_running_loop().create_task(self._probe_loop())

        # A start that timed out has left the state STOPPED: close at once
        with self.lock:
            abandoned = self.state != EngineState.STARTING
        started.set()
        if not abandoned:
            await self.closing.wait()

        # Closing: every task still running is cancelled, then the clients close.
        here = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not here]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for pool in self.pools:
            await pool.client.aclose()
        if self.health_client is not None:
            await self.health_client.aclose()
            self.health_client = None

    def stop(self) -> None:
        with self.lock:
            if self.state == EngineState.STOPPED:
                return
            self.state = EngineState.STOPPED
            pending = list(self.tickets.values())
            self.tickets.clear()
        for ticket in pending:
            ticket.answer_all(UnitError(EngineStopped(f"{self.name} stopped")))

        # The loop closes through _close_now, which also covers a loop still starting
        if self.loop is not None:
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._close_now)
        if self.thread is not None:
            self.thread.join(5.0)

    def ready(self) -> bool:
        """Up and past any block; with ``health_path``, an unhealthy unit (after a
        ``UnitError``) also needs a health GET to succeed. A healthy unit is never probed."""
        with self.lock:
            if self.state != EngineState.UP or time.monotonic() < self.blocked_until:
                return False
            if self.health_path is None or self.healthy:
                return True
            kick = not self.health_pending
            self.health_pending = True
        if kick:
            asyncio.run_coroutine_threadsafe(self._health(), self.loop)
        return False

    async def _health(self) -> None:
        ok = False
        try:
            response = await self.health_client.get(self.health_path)
            ok = response.status_code < 400
        except Exception:  # noqa: BLE001
            ok = False
        finally:
            # A probe cancelled by stop also clears the pending flag
            with self.lock:
                self.healthy = ok
                self.health_pending = False

    async def _probe_loop(self) -> None:
        while True:
            try:
                probed = dict(await asyncio.to_thread(self.load_probe))
            except Exception as exc:  # noqa: BLE001
                probed = {"probe_error": repr(exc)}
            with self.lock:
                self.probed = probed
            await asyncio.sleep(self.probe_interval_s)

    # ------------------------------------------------------------ reporting
    def spec(self) -> dict:
        return {
            "engine": "WebCallEngine",
            "name": self.name,
            "base_url": self.base_url,
            "max_inflight": self.max_inflight,
            "rate": self.rate,
            "timeout_s": self.timeout_s,
        }

    def capacity(self) -> int:
        with self.lock:
            if self.state != EngineState.UP or time.monotonic() < self.blocked_until:
                return 0
            free = self.max_inflight - self.inflight
            unpaced = len(self.unpaced)
        if free > 0 and self.bucket is not None and self.bucket.available() - unpaced < 1.0:
            return 0
        return max(0, free)

    def wake_at(self) -> float | None:
        """Monotonic time when capacity or ``ready()`` may change without a completion."""
        now = time.monotonic()
        with self.lock:
            if self.state != EngineState.UP:
                return None
            blocked = self.blocked_until if self.blocked_until > now else None
            free = self.max_inflight - self.inflight
            unpaced = len(self.unpaced)
        refill = None
        if free > 0 and self.bucket is not None:
            refill = self.bucket.due(unpaced + 1.0)
        times = [t for t in (blocked, refill) if t is not None]
        return min(times) if times else None

    def load(self) -> Load:
        with self.lock:
            extra = {
                **self.counts,
                "latency_ms": self.latency * 1000.0,
                "errors": dict(self.errors),
                "probe": dict(self.probed),
            }
            return Load(running=self.inflight, waiting=0, extra=extra)

    # ------------------------------------------------------------ work
    def submit(self, work: Any, done: Any) -> Ticket:
        ticket = Ticket([work], done)
        with self.lock:
            up = self.state == EngineState.UP
            if up:
                self.inflight += 1
                self.counts["items"] += 1
                self.tickets[ticket.id] = ticket
                if self.bucket is not None:
                    self.unpaced.add(ticket.id)
            state = self.state
        if up:
            self._post(self._launch, ticket)
            return ticket
        ticket.answer_all(UnitError(EngineStopped(f"{self.name} is {state}")))
        return ticket

    def cancel(self, ticket: Any) -> None:
        """Answer ``Cancelled`` now; a request not yet sent is never sent, one already sent runs
        to its end with its answer dropped (its slot frees then)."""
        if not isinstance(ticket, Ticket) or not ticket.answer_all(Cancelled()):
            return
        with self.lock:
            self.counts["cancelled"] += 1

    def _post(self, fn: Callable, ticket: Ticket) -> None:
        """Queue ``fn(ticket)`` for the I/O loop; one wake-up serves every queued call."""
        with self.lock:
            self.inbox.append((fn, ticket))
            kick = not self.kicked
            self.kicked = True
        if not kick:
            return
        try:
            self.loop.call_soon_threadsafe(self._drain)
        except RuntimeError:
            with self.lock:
                calls, self.inbox = self.inbox, []
                self.kicked = False
            for fn, queued in calls:
                if fn == self._launch:
                    self._settle(queued, UnitError(EngineStopped(f"{self.name} stopped")), None)

    def _drain(self) -> None:
        with self.lock:
            calls, self.inbox = self.inbox, []
            self.kicked = False
        for fn, ticket in calls:
            fn(ticket)

    def _launch(self, ticket: Ticket) -> None:
        if ticket.answered:
            self._settle(ticket, Cancelled(), None)
            return
        ticket.handle = asyncio.get_running_loop().create_task(self._run(ticket))

    async def _run(self, ticket: Ticket) -> None:
        t0 = time.perf_counter()
        try:
            result = await self._call(ticket)
        except asyncio.CancelledError:
            result = Cancelled()
        self._settle(ticket, result, time.perf_counter() - t0)

    async def _call(self, ticket: Ticket) -> Any:
        # An answered ticket (cancelled, or taken by a unit error) sends nothing
        if ticket.answered:
            return Cancelled()
        item = ticket.items[0]

        # The rate token first, so a request waiting for one holds no connection
        if self.bucket is not None:
            await self.bucket.acquire()
            with self.lock:
                self.unpaced.discard(ticket.id)
            if ticket.answered:
                return Cancelled()

        pool = self._acquire()
        try:
            request = item.payload()
            response = await pool.client.request(
                request.method,
                request.path,
                json=request.json,
                content=request.body,
                headers=request.headers or None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return self.classify(exc) or ItemError(exc)
        finally:
            self._release(pool)
        judged = self.classify(response)
        return judged if judged is not None else finish(item, response)

    def _acquire(self) -> _Pool:
        """A pool with a free connection (the least loaded one when none is free)."""
        pool = self.free.pop() if self.free else min(self.pools, key=lambda p: p.inflight)
        pool.inflight += 1
        return pool

    def _release(self, pool: _Pool) -> None:
        pool.inflight -= 1
        if pool.inflight < pool.size:
            self.free.append(pool)

    def _block_s(self, after: float | None) -> float:
        """Seconds a ``Retry-After`` blocks the unit: at most ``max_retry_after_s``."""
        if after is None or not math.isfinite(after):
            return self.max_retry_after_s if after is not None and after > 0 else 0.0
        return min(max(0.0, after), self.max_retry_after_s)

    def _settle(self, ticket: Ticket, result: Any, seconds: float | None) -> None:
        """Release the ticket's slot, record the outcome, answer it.

        A ``UnitError`` blocks the unit; under ``on_unit_error="cancel"`` it also answers
        every other request in flight with the same error. Running requests are never
        aborted (aborting one mid-connect leaves its pooled connection stuck): each runs to
        its end, its answer is dropped if the ticket was answered, and its slot frees then. A
        ticket of an earlier run (answered by ``stop``) frees no slot of this run.
        """
        with self.lock:
            current = self.tickets.pop(ticket.id, None) is not None
            if current:
                self.inflight -= 1
                self.unpaced.discard(ticket.id)
        if not current:
            ticket.answer([result])
            return

        # Under "cancel", a unit error answers every other request in flight at once
        others: list[Ticket] = []
        if isinstance(result, UnitError) and self.on_unit_error == "cancel":
            with self.lock:
                others = [t for t in self.tickets.values() if not t.answered]

        with self.lock:
            match result:
                case Ok():
                    self.counts["ok"] += 1
                    if seconds is not None:
                        a = 0.2 if self.latency else 1.0
                        self.latency = (1.0 - a) * self.latency + a * seconds
                case ItemError(error=error):
                    key = type(error).__name__
                    self.errors[key] = self.errors.get(key, 0) + 1
                case UnitError(error=error, retry_after=after):
                    self.counts["unit_errors"] += 1
                    key = type(error).__name__
                    self.errors[key] = self.errors.get(key, 0) + 1
                    block = self._block_s(after)
                    self.blocked_until = max(self.blocked_until, time.monotonic() + block)
                    self.healthy = False
        delivered = ticket.answer([result])

        # The other requests get the same error; their running work finishes unseen
        spread = sum(other.answer_all(result) for other in others)
        if spread:
            with self.lock:
                self.counts["unit_error_items"] += spread

        # A slot freed without an answer (cancelled or already answered) wakes the lanes
        if not delivered:
            self.wake()
