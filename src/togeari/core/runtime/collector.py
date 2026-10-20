"""The process collector threshold shared by every running dispatcher (the ``gc_threshold`` knob).

Each running loop holds at most one ask, under an owner number from ``owners``. The threshold in
force is the latest remaining ask; a short ask keeps the process threshold's later values. When
no ask is left, the process threshold comes back: the one read before the first ask, or one set
from outside this module while asks were held.

The first value is the allocation count that starts a young collection in every supported
Python. The later values follow the running Python's ``gc.set_threshold``: on 3.13 the
thresholds of the two older generations; on 3.14 (the incremental collector) the second sets how
much of the old generation each increment scans, and the third is ignored.
"""

import gc
import itertools
import threading

_lock = threading.Lock()
_wants: dict[int, tuple] = {}
_base: tuple | None = None
_applied: tuple | None = None

# Owner numbers for loops; never reused within the process
owners = itertools.count(1)


def _set(threshold: tuple) -> None:
    global _applied
    gc.set_threshold(*threshold)
    _applied = gc.get_threshold()


def ask(owner: int, want: tuple | None) -> None:
    """``owner`` asks for ``want``; None withdraws its ask."""
    global _base, _applied
    with _lock:
        # A threshold set from outside while asks are held becomes the one to restore
        current = gc.get_threshold()
        if _wants and current != _applied:
            _base = current

        match want:
            case None:
                if _wants.pop(owner, None) is None:
                    return
            case _:
                if not _wants:
                    _base = current
                _wants.pop(owner, None)
                _wants[owner] = want

        # The latest remaining ask, padded from the base; else the base itself
        if _wants:
            latest = next(reversed(_wants.values()))
            _set(latest + _base[len(latest) :])
        else:
            gc.set_threshold(*_base)
            _base = _applied = None


def held() -> dict[int, tuple]:
    """The asks held now, by owner."""
    with _lock:
        return dict(_wants)
