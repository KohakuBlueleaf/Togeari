"""The worker process of ``PythonCallEngine``'s process mode.

Messages from the engine, over one duplex connection:

- ``("run", packet)``: decode a ``PythonCall``, run it, answer ``("ok", packet)``
  or ``("err", packed exception)``;
- ``("release",)``: the engine has read the last result; free its blocks;
- ``("stop",)``: exit.

The worker sends ``("ready", pid)`` once it is up, and is in the worker
registry (``engines.common.registry``) while it runs.
"""

import importlib
import os
from typing import Any

from togeari.engines.common.registry import registered
from togeari.engines.common.transport import Codec, Packet, pack_error, release


def main(conn: Any, shm: bool, threshold: int, imports: tuple = ()) -> None:
    with registered(__name__):
        serve(conn, shm, threshold, imports)


def serve(conn: Any, shm: bool, threshold: int, imports: tuple) -> None:
    codec = Codec(shm=shm, threshold=threshold)

    # Node libraries load once, before the worker reports ready.
    for name in imports:
        importlib.import_module(name)

    # Result blocks waiting for the engine's release, and input blocks still exported.
    held: list = []
    mapped: list = []
    conn.send(("ready", os.getpid()))

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break
        mapped = release(mapped, unlink=False)

        match msg:
            case ("run", Packet() as packet):
                held = release(held, unlink=True)
                reply, held, opened = _run(codec, packet)
                conn.send(reply)
                mapped.extend(release(opened, unlink=False))
            case ("release",):
                held = release(held, unlink=True)
            case ("stop",):
                break

    # Blocks this worker created are freed on the way out.
    release(held, unlink=True)


def _run(codec: Codec, packet: Packet) -> tuple[tuple, list, list]:
    """The reply, the result blocks this worker owns, and the input blocks it mapped."""
    try:
        call, opened = codec.decode(packet, copy=False)
    except Exception as exc:  # noqa: BLE001
        return ("err", pack_error(exc)), [], []

    try:
        raw = call.run()
        del call
        out, owned = codec.encode(raw)
        del raw
    except Exception as exc:  # noqa: BLE001
        return ("err", pack_error(exc)), [], opened
    return ("ok", out), owned, opened
