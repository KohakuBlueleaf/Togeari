"""Process transport: pickle protocol 5, large buffers out of band through shared memory.

A ``Codec`` turns an object into a ``Packet``: the pickle stream plus, with
``shm=True``, one shared-memory block per out-of-band buffer of at least
``threshold`` bytes: anything that pickles through ``PickleBuffer`` (numpy
arrays) and types given a reducer. ``bytes`` and ``bytearray`` always travel in
band, since pickle writes them before any reducer is consulted. Without shared
memory every buffer travels in band.

Block ownership: a block belongs to the side that created it, which closes
and unlinks it (``release``) once the other side reports it read. The other
side copies the buffers out and closes its mapping at once (``copy=True``), or
maps them in place and closes the mapping after use.
"""

import contextlib
import io
import pickle
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing import shared_memory
from typing import Any

from togeari.engines.common.errors import RemoteError

# Attach options: no resource-tracker registration for a block another process owns.
_ATTACH = {"track": False} if sys.version_info >= (3, 13) else {}

Reducer = Callable[[Any, list], Any]


@dataclass
class Packet:
    """A pickle stream and the shared-memory blocks holding its out-of-band buffers."""

    data: bytes
    # (block name, buffer size in bytes), in buffer order.
    blocks: list[tuple[str, int]] = field(default_factory=list)

    @property
    def nbytes(self) -> int:
        return len(self.data) + sum(n for _, n in self.blocks)

    @property
    def shm_bytes(self) -> int:
        return sum(n for _, n in self.blocks)


class _Pickler(pickle.Pickler):
    """A protocol-5 pickler that applies per-type reducers; ``keep`` pins objects buffers point
    into.
    """

    def __init__(self, file: Any, reducers: dict[type, Reducer], keep: list, buffer_callback: Any):
        super().__init__(file, protocol=5, buffer_callback=buffer_callback)
        self.reducers = reducers
        self.keep = keep

    def reducer_override(self, obj: Any) -> Any:
        for cls, reduce in self.reducers.items():
            if isinstance(obj, cls):
                return reduce(obj, self.keep)
        return NotImplemented


# Buffers of at least this many bytes go through shared memory (study S1, M1)
SHM_THRESHOLD = 1 << 20


class Codec:
    """Encodes objects to ``Packet``s and back, through shared memory when ``shm`` is on."""

    def __init__(
        self,
        shm: bool,
        threshold: int = SHM_THRESHOLD,
        reducers: dict[type, Reducer] | None = None,
    ):
        self.shm = shm
        self.threshold = threshold
        self.reducers = dict(reducers or {})

    def encode(self, obj: Any) -> tuple[Packet, list[shared_memory.SharedMemory]]:
        """The packet and the blocks this side now owns (release them once the reader is done)."""
        oob: list[pickle.PickleBuffer] = []

        # In band: buffers under the threshold, or every buffer without shared memory.
        def route(buf: pickle.PickleBuffer) -> bool:
            if not self.shm or buf.raw().nbytes < self.threshold:
                return True
            oob.append(buf)
            return False

        # The pickler: plain without reducers; a buffer callback only with shared memory.
        stream = io.BytesIO()
        callback = route if self.shm else None
        keep: list = []
        if self.reducers:
            _Pickler(stream, self.reducers, keep, callback).dump(obj)
        else:
            pickle.Pickler(stream, protocol=5, buffer_callback=callback).dump(obj)

        # Each out-of-band buffer is copied into a block of its own.
        owned: list[shared_memory.SharedMemory] = []
        blocks: list[tuple[str, int]] = []
        try:
            for buf in oob:
                raw = buf.raw()
                block = shared_memory.SharedMemory(create=True, size=max(1, raw.nbytes))
                owned.append(block)
                block.buf[: raw.nbytes] = raw
                blocks.append((block.name, raw.nbytes))
        except BaseException:
            release(owned, unlink=True)
            raise
        finally:
            for buf in oob:
                buf.release()
            keep.clear()
        return Packet(stream.getvalue(), blocks), owned

    def decode(self, packet: Packet, copy: bool) -> tuple[Any, list[shared_memory.SharedMemory]]:
        """The object and the blocks still mapped (empty with ``copy=True``)."""
        opened: list[shared_memory.SharedMemory] = []
        try:
            for name, _ in packet.blocks:
                opened.append(shared_memory.SharedMemory(name=name, **_ATTACH))
            if copy:
                buffers = [bytearray(b.buf[:n]) for b, (_, n) in zip(opened, packet.blocks)]
            else:
                buffers = [b.buf[:n] for b, (_, n) in zip(opened, packet.blocks)]
            obj = pickle.loads(packet.data, buffers=buffers)
        except BaseException:
            buffers = []
            release(opened, unlink=False)
            raise
        if copy:
            del buffers
            release(opened, unlink=False)
            return obj, []
        return obj, opened


def release(blocks: list[shared_memory.SharedMemory], unlink: bool) -> list:
    """Close (and, for the owner, unlink) blocks; returns those still exported by live objects."""
    held = []
    for block in blocks:
        if unlink:
            with contextlib.suppress(FileNotFoundError):
                block.unlink()
        try:
            block.close()
        except BufferError:
            held.append(block)
    return held


def pack_error(exc: BaseException) -> bytes:
    """An exception and its formatted traceback, pickled; unpicklable ones become RuntimeError."""
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        return pickle.dumps((exc, text), protocol=5)
    except Exception:  # noqa: BLE001
        return pickle.dumps((RuntimeError(f"{type(exc).__name__}: {exc}"), text), protocol=5)


def unpack_error(data: bytes) -> BaseException:
    """The exception from ``pack_error``, with the remote traceback as its cause."""
    try:
        exc, text = pickle.loads(data)
    except Exception as err:  # noqa: BLE001
        return RemoteError(f"undecodable remote error: {err!r}")
    exc.__cause__ = RemoteError(text)
    return exc
