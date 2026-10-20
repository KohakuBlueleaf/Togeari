"""Starting and stopping units: all at once, each bounded by a timeout (spec/06 §6.2)."""

import logging
import threading
import time
from typing import Any

log = logging.getLogger("togeari.machine")

# Seconds between two ready() checks while a unit starts
READY_POLL_S = 0.05


def start_all(units: list, timeout: float) -> list[tuple[Any, str]]:
    """Start every unit on its own thread and wait at most ``timeout``; (unit, problem) per failure.

    A unit whose ``start()`` hangs is reported as timed out and left to its
    (daemon) thread.
    """
    units = list({id(u): u for u in units}.values())
    if not units:
        return []
    results: dict[int, str | None] = {}

    def start(u: Any) -> None:
        try:
            u.start()
            deadline = time.monotonic() + timeout
            while not u.ready():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"not ready after {timeout}s")
                time.sleep(READY_POLL_S)
            results[id(u)] = None
        except Exception as exc:  # noqa: BLE001
            results[id(u)] = f"start() failed: {exc}"

    threads = [
        threading.Thread(target=start, args=(u,), name=f"togeari-start-{u.name}", daemon=True)
        for u in units
    ]
    for t in threads:
        t.start()
    end = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))

    failures = []
    for u in units:
        if id(u) not in results:
            failures.append((u, f"start() timed out after {timeout}s"))
        elif results[id(u)] is not None:
            failures.append((u, results[id(u)]))
    return failures


def stop_all(units: list) -> None:
    """Stop every unit, whatever each one's stop() does."""
    for u in {id(u): u for u in units}.values():
        try:
            u.stop()
        except Exception:
            log.warning("unit %s: stop() failed", getattr(u, "name", "?"), exc_info=True)
