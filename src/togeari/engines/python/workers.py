"""Worker slots of ``PythonCallEngine``: one thread, or one thread driving one process.

A slot takes tickets from its engine one at a time, runs the item and settles
it through the engine. A process slot restarts its process when the process
dies under an item or is killed by a cancel; a slot whose process cannot
restart leaves the engine.
"""

import contextlib
import multiprocessing
import threading
import time
from typing import Any

from togeari.core.contracts.results import ItemError
from togeari.engines.common.errors import WorkerDied
from togeari.engines.common.ticket import finish
from togeari.engines.common.transport import Codec, Packet, release, unpack_error
from togeari.engines.python import child

_SPAWN = multiprocessing.get_context("spawn")


def run_here(item: Any) -> Any:
    """Run a ``PythonCall`` item in this thread: ``Ok`` or ``ItemError``."""
    try:
        raw = item.payload().run()
    except Exception as exc:  # noqa: BLE001
        return ItemError(exc)
    return finish(item, raw)


class ThreadSlot:
    """A worker thread in the machine's process.

    ``busy``, ``current`` and ``exit`` belong to the engine's lock; ``epoch`` is the
    engine start the slot was opened for.
    """

    killed = False
    proc = None
    epoch = 0

    def __init__(self, engine: Any, index: int):
        self.engine = engine
        self.busy = False
        self.current: Any = None
        self.exit = False
        self.thread = threading.Thread(
            target=self.loop, name=f"{engine.name}-thread{index}", daemon=True
        )

    def start(self) -> bool:
        self.thread.start()
        return True

    def interrupt(self) -> None:
        """A running function cannot be stopped; the slot stays busy until it returns."""

    def loop(self) -> None:
        engine = self.engine
        while (ticket := engine.take(self)) is not None:
            engine.settle(self, ticket, run_here(ticket.items[0]), died=False)
            if not engine.stays(self):
                break

    def join(self, timeout: float) -> None:
        self.thread.join(timeout)


class ProcessSlot:
    """A worker process and the thread in the machine's process that drives it.

    ``busy``, ``current``, ``killed`` and ``exit`` belong to the engine's lock;
    ``epoch`` is the engine start the slot was opened for.
    """

    epoch = 0

    def __init__(self, engine: Any, index: int, codec: Codec, start_timeout_s: float):
        self.engine = engine
        self.index = index
        self.codec = codec
        self.start_timeout_s = start_timeout_s
        self.proc: Any = None
        self.conn: Any = None
        self.busy = False
        self.current: Any = None
        self.killed = False
        self.exit = False
        self.thread = threading.Thread(
            target=self.loop, name=f"{engine.name}-proc{index}", daemon=True
        )

    # ------------------------------------------------------------ process
    def spawn(self) -> None:
        """Start the process without waiting for it."""
        here, there = _SPAWN.Pipe(duplex=True)
        proc = _SPAWN.Process(
            target=child.main,
            args=(there, self.codec.shm, self.codec.threshold, self.engine.imports),
            name=f"{self.engine.name}-worker{self.index}",
            daemon=True,
        )
        proc.start()
        there.close()
        self.proc, self.conn = proc, here

    def wait_ready(self) -> bool:
        """True once the process reported ready within the start timeout."""
        try:
            if not self.conn.poll(self.start_timeout_s):
                raise TimeoutError
            msg = self.conn.recv()
        except (EOFError, OSError, TimeoutError):
            self.kill()
            return False
        return isinstance(msg, tuple) and msg[0] == "ready"

    def start(self) -> bool:
        if self.proc is None:
            self.spawn()
        if not self.wait_ready():
            return False
        self.thread.start()
        return True

    def respawn(self) -> bool:
        self.kill()
        try:
            self.spawn()
        except Exception:  # noqa: BLE001
            return False
        return self.wait_ready()

    def interrupt(self) -> None:
        """Kill the process under the running item (the caller holds the engine's lock)."""
        self.killed = True
        if self.proc is not None:
            self.proc.kill()

    def kill(self) -> None:
        if self.proc is not None and self.proc.is_alive():
            self.proc.kill()
        if self.proc is not None:
            self.proc.join(5.0)
        if self.conn is not None:
            self.conn.close()

    # ------------------------------------------------------------ work
    def loop(self) -> None:
        engine = self.engine
        while (ticket := engine.take(self)) is not None:
            # A process that exited while idle hands the ticket back and leaves.
            if not self.proc.is_alive():
                engine.give_back(self, ticket)
                engine.idle_death(self, self.proc)
                break

            result, died = self.run(ticket.items[0])
            if not engine.settle(self, ticket, result, died):
                if not engine.stays(self):
                    break
                continue

            # The process restarts; the slot is free once it is back
            if not self.respawn():
                engine.slot_lost(self)
                return
            if engine.monitor is not None:
                engine.monitor.poke()
            if not engine.release(self):
                break
        self.close()

    def run(self, item: Any) -> tuple[Any, bool]:
        """The item's result, and whether the process died under it."""
        try:
            packet, owned = self.codec.encode(item.payload())
        except Exception as exc:  # noqa: BLE001
            return ItemError(exc), False
        self.engine.count("shm_bytes", packet.shm_bytes)

        try:
            self.conn.send(("run", packet))
            reply = self.conn.recv()
        except (EOFError, OSError):
            code = self.proc.exitcode if self.proc is not None else None
            return ItemError(WorkerDied(f"worker exited ({code})")), True
        finally:
            release(owned, unlink=True)
        return self.result(item, reply), False

    def result(self, item: Any, reply: Any) -> Any:
        match reply:
            case ("ok", Packet() as packet):
                try:
                    raw, _ = self.codec.decode(packet, copy=True)
                except Exception as exc:  # noqa: BLE001
                    return ItemError(exc)
                finally:
                    self.release_reply(packet)
                self.engine.count("shm_bytes", packet.shm_bytes)
                return finish(item, raw)
            case ("err", bytes() as data):
                return ItemError(unpack_error(data))
            case _:
                return ItemError(RuntimeError(f"unexpected worker reply {reply!r}"))

    def release_reply(self, packet: Packet) -> None:
        """Tell the process the reply's blocks have been read."""
        if not packet.blocks:
            return
        with contextlib.suppress(EOFError, OSError):
            self.conn.send(("release",))

    def close(self) -> None:
        """Ask the process to exit, then make sure it did."""
        with contextlib.suppress(EOFError, OSError):
            self.conn.send(("stop",))
        deadline = time.monotonic() + 2.0
        while self.proc.is_alive() and time.monotonic() < deadline:
            self.proc.join(0.05)
        self.kill()

    def join(self, timeout: float) -> None:
        if self.thread.is_alive():
            self.thread.join(timeout)
        if self.thread.is_alive():
            self.kill()
            self.thread.join(timeout)
