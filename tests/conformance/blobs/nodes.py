"""The node library: it uses ``Blob`` but never imports the digest registration."""

import threading

from kohakunode import NodeLibrary

from blobs.kinds import Blob

LIB = NodeLibrary("blobs")


@LIB.node()
def size(b: Blob) -> int:
    return len(b.data)


@LIB.node()
def guard(x: int) -> object:
    """A value that does not pickle: its digests are weak."""
    return threading.Lock()


@LIB.node()
def held(lock: object) -> int:
    return 1
