"""One statement as an engine sees it."""

import hashlib
import pickle
import threading
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.digest import digest

# Guards the creation of an item's own payload build lock
_GUARD = threading.Lock()


class WorkItem:
    """Engines use only this surface: ``payload()``, ``finish(raw)``, ``digest()``.

    ``payload()`` runs the adapter's ``prepare`` once and caches it, so an engine
    may call it on its own thread. ``finish(raw)`` runs the adapter's ``decode``.
    ``deadline`` is a ``time.monotonic()`` time after which the item times out.
    """

    __slots__ = (
        "node",
        "priority",
        "deadline",
        "inputs",
        "adapter",
        "protocol",
        "key",
        "seq",
        "_payload",
        "_has_payload",
        "_build",
        "_digest",
        "owner",
        "ext",
    )

    def __init__(
        self,
        node: str,
        inputs: dict[str, Any],
        adapter: Any,
        protocol: str | None = None,
        priority: int = 0,
        deadline: float | None = None,
        key: Any = None,
        owner: Any = None,
    ):
        self.node = node
        self.inputs = inputs
        self.adapter = adapter
        self.protocol = protocol
        self.priority = priority
        self.deadline = deadline
        self.key = key
        self.seq = 0
        self._payload: Any = None
        self._has_payload = False
        self._build: threading.Lock | None = None
        self._digest: bytes | None = None
        # The machine's inflight and the lane's bookkeeping; engines never look at them
        self.owner = owner
        self.ext: dict = {}

    def payload(self) -> Any:
        if self._has_payload:
            return self._payload

        # The item's own build lock, made once; items never share one
        lock = self._build
        if lock is None:
            with _GUARD:
                lock = self._build
                if lock is None:
                    lock = self._build = threading.Lock()

        # The first caller builds the payload; concurrent callers of this item wait for it
        with lock:
            if not self._has_payload:
                self._payload = self.adapter.prepare(self.inputs, self.protocol)
                self._has_payload = True
        return self._payload

    def finish(self, raw: Any) -> Any:
        return self.adapter.decode(raw, self.protocol)

    def release(self) -> None:
        """Drop the inputs and the built payload once the item's result is settled; a later
        ``payload()`` returns None."""
        self.inputs = None
        self._payload = None
        self._has_payload = True

    def digest(self) -> bytes:
        if self._digest is None:
            self._digest = digest((self.node, self.inputs))
        return self._digest


def payload_hash(obj: Any) -> str:
    """A short hex hash of any picklable object (store keys, reports)."""
    try:
        return hashlib.sha256(pickle.dumps(obj, protocol=5)).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return hashlib.sha256(type(obj).__qualname__.encode()).hexdigest()[:16]


Done = Callable[[Any, list], None]
