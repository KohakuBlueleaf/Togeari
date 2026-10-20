"""A peer process reached through two pipes, objects encoded by ``Codec``.

``shm=False`` is pickle through the pipe; ``shm=True`` puts buffers of at least
``threshold`` bytes in shared memory. The receiving side copies buffers out
(``copy=True``) or maps them in place (``copy=False``). Blocks are released by
their creator once the other side has acknowledged them, outside the timings.
"""

import contextlib
import multiprocessing
import time
from collections import deque
from typing import Any

from togeari.engines.common.transport import Codec, release

_SPAWN = multiprocessing.get_context("spawn")


def peer_main(requests: Any, replies: Any, shm: bool, threshold: int, copy: bool) -> None:
    """The peer process: decode what arrives, acknowledge it, echo it when asked."""
    codec = Codec(shm=shm, threshold=threshold)
    held: list = []
    while True:
        try:
            msg = requests.recv()
        except (EOFError, OSError):
            break

        match msg:
            # Timed one-way and echoed messages
            case ("one", packet, _):
                obj, opened = codec.decode(packet, copy=copy)
                t = time.perf_counter()
                del obj
                release(opened, unlink=False)
                replies.send(("t", t))
            case ("echo", packet):
                obj, opened = codec.decode(packet, copy=copy)
                back, held = codec.encode(obj)
                del obj
                release(opened, unlink=False)
                replies.send(("echo", back))

            # Control
            case ("release",):
                held = release(held, unlink=True)
            case ("cpu",):
                replies.send(("cpu", time.process_time()))
            case ("stop",):
                break
    release(held, unlink=True)


class PipeLink:
    def __init__(self, shm: bool, copy: bool, threshold: int = 64 * 1024):
        self.name = ("shm-copy" if copy else "shm-map") if shm else "pipe"
        self.shm = shm
        self.copy = copy
        self.codec = Codec(shm=shm, threshold=threshold)
        self.proc: Any = None
        self.req: Any = None
        self.rep: Any = None

    def open(self) -> None:
        req_out, req_in = _SPAWN.Pipe(duplex=False)
        rep_out, rep_in = _SPAWN.Pipe(duplex=False)
        self.proc = _SPAWN.Process(
            target=peer_main,
            args=(req_out, rep_in, self.shm, self.codec.threshold, self.copy),
            name=f"s1-peer-{self.name}",
            daemon=True,
        )
        self.proc.start()
        req_out.close()
        rep_in.close()
        self.req, self.rep = req_in, rep_out

    def oneway(self, obj: object) -> float:
        t0 = time.perf_counter()
        packet, owned = self.codec.encode(obj)
        self.req.send(("one", packet, t0))
        _, t = self.rep.recv()
        release(owned, unlink=True)
        return t - t0

    def echo(self, obj: object) -> float:
        t0 = time.perf_counter()
        packet, owned = self.codec.encode(obj)
        self.req.send(("echo", packet))
        _, back = self.rep.recv()
        got, opened = self.codec.decode(back, copy=self.copy)
        dt = time.perf_counter() - t0

        # Blocks of both directions are freed outside the timing
        del got
        release(opened, unlink=False)
        release(owned, unlink=True)
        self.req.send(("release",))
        return dt

    def stream(self, objs: list, window: int) -> float:
        t0 = time.perf_counter()
        pending: deque = deque()
        for obj in objs:
            if len(pending) >= window:
                self.rep.recv()
                release(pending.popleft(), unlink=True)
            packet, owned = self.codec.encode(obj)
            self.req.send(("one", packet, time.perf_counter()))
            pending.append(owned)
        while pending:
            self.rep.recv()
            release(pending.popleft(), unlink=True)
        return time.perf_counter() - t0

    def peer_cpu(self) -> float | None:
        self.req.send(("cpu",))
        _, cpu = self.rep.recv()
        return cpu

    def close(self) -> None:
        with contextlib.suppress(EOFError, OSError):
            self.req.send(("stop",))
        self.proc.join(10.0)
        if self.proc.is_alive():
            self.proc.kill()
            self.proc.join(5.0)
