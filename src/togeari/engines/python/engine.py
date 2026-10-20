"""``PythonCallEngine``: runs ``PythonCall`` payloads on worker threads or worker processes."""

import itertools
import threading
from collections import OrderedDict
from typing import Any

from togeari.core.contracts.engine import EngineBase, Load
from togeari.core.contracts.results import Cancelled, ItemError, Ok, UnitError
from togeari.engines.common.errors import EngineStopped
from togeari.engines.common.state import EngineState as State
from togeari.engines.common.ticket import Ticket
from togeari.engines.common.transport import SHM_THRESHOLD, Codec
from togeari.engines.python.monitor import Monitor
from togeari.engines.python.workers import ProcessSlot, ThreadSlot


class PythonCallEngine(EngineBase):
    """A call engine over ``workers`` slots; ``capacity()`` is the number of free slots.

    ``mode="thread"`` runs each call on a worker thread in the machine's process
    (``shares_memory`` True). ``mode="process"`` runs it in a spawned worker
    process (``shares_memory`` False); the payload crosses by pickle through a
    pipe (``transport="pickle"``) or with buffers of at least ``shm_threshold``
    bytes in shared memory (``transport="shm"``). Each worker process imports
    the ``imports`` modules (node libraries, by module path) once at start;
    functions cross by reference (module and name), so they must be importable
    top-level functions.

    Errors: an exception in the function, or a worker process dying while it
    runs the item, is that item's ``ItemError`` (the process is restarted). A
    worker process that exits while idle leaves the engine and is replaced in
    the background; with no worker left the engine is down, waiting and new
    items get ``UnitError``, and ``ready()`` starts the replacements.

    Cancel: a waiting item leaves the queue. A running item in process mode has
    its worker process killed; the slot restarts it and counts as busy until the
    new process is ready. A running item in thread mode cannot be stopped; its
    slot stays busy until the function returns.
    """

    kind = "call"
    protocol = "python"

    def __init__(
        self,
        workers: int = 8,
        mode: str = "thread",
        transport: str = "shm",
        shm_threshold: int = SHM_THRESHOLD,
        start_timeout_s: float = 60.0,
        stop_timeout_s: float = 5.0,
        imports: tuple[str, ...] | list[str] = (),
        name: str | None = None,
    ):
        super().__init__(name or "python")
        match mode, transport:
            case ("thread" | "process"), ("pickle" | "shm"):
                pass
            case _:
                raise ValueError(f"mode {mode!r} / transport {transport!r} not supported")
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.workers = workers
        self.mode = mode
        self.transport = transport
        self.shares_memory = mode == "thread"
        self.codec = Codec(shm=transport == "shm", threshold=shm_threshold)
        self.start_timeout_s = start_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self.imports = tuple(imports)
        self.serial = itertools.count()

        # Shared state, guarded by ``lock`` (a condition the idle slots wait on).
        self.lock = threading.Condition()
        self.state = State.STOPPED
        self.pending: OrderedDict[int, Ticket] = OrderedDict()
        self.slots: list = []
        self.running = 0
        self.reviving = False
        # Starts so far; each slot carries the start (epoch) it was opened for.
        self.epoch = 0
        self.monitor: Monitor | None = None
        self.counts = {
            "items": 0,
            "ok": 0,
            "errors": 0,
            "cancelled": 0,
            "restarts": 0,
            "kills": 0,
            "shm_bytes": 0,
        }

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the slots of a new epoch; its counts of running and waiting items start at 0.

        Slots of an earlier epoch still finishing an item belong to that epoch:
        they change no count of this one.
        """
        with self.lock:
            if self.state != State.STOPPED:
                return
            self.epoch += 1
            self.running = 0
            self.pending.clear()
            self.reviving = False
            epoch = self.epoch
        slots = self._open(self.workers, epoch)
        with self.lock:
            self.slots = slots
            self.state = State.UP if slots else State.DOWN
        if self.mode == "process":
            self.monitor = Monitor(self)

    def _open(self, n: int, epoch: int) -> list:
        """Start ``n`` slots of ``epoch``; returns those that came up."""
        match self.mode:
            case "thread":
                slots = [ThreadSlot(self, next(self.serial)) for _ in range(n)]
            case _:
                slots = [
                    ProcessSlot(self, next(self.serial), self.codec, self.start_timeout_s)
                    for _ in range(n)
                ]
        for slot in slots:
            slot.epoch = epoch

        # Processes boot together; each slot then waits for its own.
        if self.mode == "process":
            for slot in slots:
                slot.spawn()
        return [slot for slot in slots if slot.start()]

    def _current(self, slot: Any) -> bool:
        """The slot belongs to the current epoch (under ``lock``)."""
        return slot.epoch == self.epoch

    def ready(self) -> bool:
        with self.lock:
            if self.state == State.UP:
                return True
            revive = self.state == State.DOWN and not self.reviving
            self.reviving = self.reviving or revive
        if revive:
            self._revive_later()
        return False

    def _revive_later(self) -> None:
        threading.Thread(target=self._revive, name=f"{self.name}-revive", daemon=True).start()

    def _revive(self) -> None:
        """Replace lost slots."""
        with self.lock:
            missing = self.workers - len(self.slots)
            epoch = self.epoch
        slots = self._open(missing, epoch) if missing > 0 else []
        with self.lock:
            if epoch == self.epoch:
                self.reviving = False
            stale = slots if self.state == State.STOPPED or epoch != self.epoch else []
            if not stale:
                self.slots.extend(slots)
                if self.slots:
                    self.state = State.UP
            for slot in stale:
                slot.exit = True
            self.lock.notify_all()
        if self.monitor is not None:
            self.monitor.poke()
        if slots and not stale:
            self.wake()

    def stop(self) -> None:
        with self.lock:
            if self.state == State.STOPPED and not self.slots:
                return
            self.state = State.STOPPED
            slots, self.slots = self.slots, []
            waiting = list(self.pending.values())
            self.pending.clear()
            running = [slot.current for slot in slots if slot.current is not None]
            for slot in slots:
                slot.exit = True
            self.lock.notify_all()
            monitor, self.monitor = self.monitor, None

        # Waiting and running items are answered now; workers exit after their current item.
        for ticket in waiting + running:
            ticket.answer_all(UnitError(EngineStopped(f"{self.name} stopped")))
        if monitor is not None:
            monitor.close()
        for slot in slots:
            slot.join(self.stop_timeout_s)

    # ------------------------------------------------------------ reporting
    def spec(self) -> dict:
        return {
            "engine": "PythonCallEngine",
            "name": self.name,
            "mode": self.mode,
            "transport": self.transport,
            "workers": self.workers,
            "imports": list(self.imports),
        }

    def capacity(self) -> int:
        with self.lock:
            if self.state != State.UP:
                return 0
            return max(0, len(self.slots) - self.running - len(self.pending))

    def load(self) -> Load:
        with self.lock:
            extra = {"live": len(self.slots), "state": self.state, **self.counts}
            return Load(running=self.running, waiting=len(self.pending), extra=extra)

    def count(self, key: str, n: int) -> None:
        with self.lock:
            self.counts[key] += n

    # ------------------------------------------------------------ work
    def submit(self, work: Any, done: Any) -> Ticket:
        ticket = Ticket([work], done)
        with self.lock:
            up = self.state == State.UP
            if up:
                self.counts["items"] += 1
                self.pending[ticket.id] = ticket
                self.lock.notify()
            state = self.state
        if not up:
            ticket.answer_all(UnitError(EngineStopped(f"{self.name} is {state}")))
        return ticket

    def cancel(self, ticket: Any) -> None:
        if not isinstance(ticket, Ticket) or not ticket.answer_all(Cancelled()):
            return
        with self.lock:
            self.counts["cancelled"] += 1
            self.pending.pop(ticket.id, None)
            slot = ticket.handle
            if slot is not None and slot.current is ticket:
                slot.interrupt()

    # ------------------------------------------------------------ slot side
    def take(self, slot: Any) -> Ticket | None:
        """The next ticket for ``slot`` to run; None when the slot should exit."""
        with self.lock:
            while not slot.exit and self._current(slot) and not self.pending:
                self.lock.wait()
            if slot.exit or not self._current(slot):
                # The wake-up this slot consumed goes on to a slot that stays.
                if self.pending:
                    self.lock.notify()
                return None
            _, ticket = self.pending.popitem(last=False)
            self.running += 1
            slot.busy = True
            slot.current = ticket
            ticket.handle = slot
            return ticket

    def give_back(self, slot: Any, ticket: Ticket) -> None:
        """Return a taken ticket to the front of the queue (its slot cannot run it)."""
        with self.lock:
            if self._current(slot):
                self.running -= 1
            slot.busy = False
            slot.current = None
            if not ticket.answered and self._current(slot):
                self.pending[ticket.id] = ticket
                self.pending.move_to_end(ticket.id, last=False)
                self.lock.notify()

    def settle(self, slot: Any, ticket: Ticket, result: Any, died: bool) -> bool:
        """Answer the slot's current ticket; True when its process must restart.

        A slot that goes on is free before the answer goes out; a slot that restarts
        stays busy (counted as running) until ``release``.
        """
        with self.lock:
            slot.current = None
            killed, slot.killed = slot.killed, False
            if killed:
                self.counts["kills"] += 1
                result = Cancelled()
            elif died:
                self.counts["restarts"] += 1
            match result:
                case Ok():
                    self.counts["ok"] += 1
                case ItemError():
                    self.counts["errors"] += 1
            restart = (killed or died) and not slot.exit
            if not restart:
                self.running -= self._current(slot)
                slot.busy = False
            answered_early = ticket.answered
        ticket.answer([result])

        # A cancelled item's slot frees without a completion
        if answered_early and not restart:
            self.wake()
        return restart

    def stays(self, slot: Any) -> bool:
        """False when the slot should exit."""
        with self.lock:
            return not slot.exit

    def release(self, slot: Any) -> bool:
        """A restarted slot is free again; False when it should exit."""
        with self.lock:
            self.running -= self._current(slot)
            slot.busy = False
            stays = not slot.exit
        self.wake()
        return stays

    def idle_death(self, slot: Any, proc: Any) -> bool:
        """``proc`` exited while ``slot`` was idle: the slot leaves; False when it was not idle."""
        with self.lock:
            if slot.busy or slot.proc is not proc or slot not in self.slots:
                return False
            slot.exit = True
            self.slots.remove(slot)
            self.counts["restarts"] += 1
            self.lock.notify_all()
        self._shrink()
        return True

    def slot_lost(self, slot: Any) -> None:
        """A busy slot whose process could not restart leaves the engine."""
        with self.lock:
            self.running -= self._current(slot)
            slot.busy = False
            slot.exit = True
            if slot in self.slots:
                self.slots.remove(slot)
        self._shrink()

    def _shrink(self) -> None:
        """After a slot left: replace it now, or with none left go down until ``ready()``."""
        with self.lock:
            if self.state != State.UP:
                return
            waiting = []
            if not self.slots:
                self.state = State.DOWN
                waiting = list(self.pending.values())
                self.pending.clear()
            revive = bool(self.slots) and not self.reviving
            self.reviving = self.reviving or revive
        for ticket in waiting:
            ticket.answer_all(UnitError(EngineStopped(f"{self.name}: no worker running")))
        if revive:
            self._revive_later()
