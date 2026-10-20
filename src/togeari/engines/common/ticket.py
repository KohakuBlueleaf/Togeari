"""Tickets: one submit, its items, its ``done`` callback, answered exactly once."""

import itertools
import threading
from collections.abc import Callable
from typing import Any

from togeari.core.contracts.results import ItemError, Ok

_ids = itertools.count(1)


class Ticket:
    """One submit; ``answer`` delivers its results through ``done`` at most once.

    ``handle`` holds whatever the engine uses to reach the running work (a task,
    a job); ``answered`` is True once a result list has been delivered.
    """

    __slots__ = ("id", "items", "done", "handle", "answered", "_lock")

    def __init__(self, items: list, done: Callable[[Any, list], None]):
        self.id = next(_ids)
        self.items = items
        self.done = done
        self.handle: Any = None
        self.answered = False
        self._lock = threading.Lock()

    def answer(self, results: list) -> bool:
        """Deliver ``results`` (one per item); False when the ticket was already answered."""
        with self._lock:
            if self.answered:
                return False
            self.answered = True
        self.done(self, results)
        return True

    def answer_all(self, result: Any) -> bool:
        """Deliver the same result for every item."""
        return self.answer([result] * len(self.items))

    def __repr__(self) -> str:
        return f"Ticket({self.id}, items={len(self.items)}, answered={self.answered})"


def finish(item: Any, raw: Any) -> Ok | ItemError:
    """Decode ``raw`` through the item's adapter: ``Ok(outputs, True)``, or ``ItemError``."""
    try:
        return Ok(item.finish(raw), True)
    except Exception as exc:  # noqa: BLE001
        return ItemError(exc)
