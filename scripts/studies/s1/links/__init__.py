"""Transports between a sender and a peer, for M1.

Every link has the same surface:

- ``open()`` / ``close()``;
- ``oneway(obj)``: seconds from the start of sending to the object decoded at the peer;
- ``echo(obj)``: seconds for the object to reach the peer and come back decoded;
- ``stream(objs, window)``: seconds to hand over every object, ``window`` unacknowledged at most;
- ``peer_cpu()``: the peer process's CPU seconds so far (None when the peer is a thread here).
"""

from s1.links.http import HttpLink
from s1.links.pipe import PipeLink
from s1.links.thread import ThreadLink


def make(name: str) -> object:
    match name:
        case "thread":
            return ThreadLink()
        case "pipe":
            return PipeLink(shm=False, copy=True)
        case "shm-copy":
            return PipeLink(shm=True, copy=True)
        case "shm-map":
            return PipeLink(shm=True, copy=False)
        case "http":
            return HttpLink()
        case _:
            raise ValueError(f"link {name!r}")
