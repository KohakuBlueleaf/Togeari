"""The writer's own process: ``python -m togeari.plugins.telemetry.child``.

Stdin: JSON lines, first ``{"config": {"ring", "root", "knobs"}}``, then the writer's messages
(``writer.py``) and finally ``{"stop": true}``. Stdout: ``{"ready": true}`` once the ring and
the files are open, then ``{"stats": ...}`` after every step. Every ``flush_s`` one step takes
the messages that arrived. At stop, or at end of input (the machine's process gone), a last
step closes every bucket, then the files. Interrupts are left to the machine's process.
"""

import json
import logging
import queue
import signal
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from togeari.core.plugin.base import open_ring
from togeari.plugins.telemetry.writer import Writer


def read(inbox: queue.Queue, done: threading.Event) -> None:
    try:
        for line in sys.stdin.buffer:
            msg = json.loads(line)
            if msg.get("stop"):
                break
            inbox.put(msg)
    finally:
        done.set()


def take(inbox: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(inbox.get_nowait())
        except queue.Empty:
            return out


def say(msg: dict) -> None:
    """A line to the machine's process; nobody reading is not an error (it is gone)."""
    try:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()
    except OSError:
        pass


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    first = json.loads(sys.stdin.buffer.readline())["config"]
    cfg = SimpleNamespace(**first["knobs"])
    ring = open_ring(first["ring"])
    writer = Writer(cfg, ring, Path(first["root"]))
    say({"ready": True})

    inbox: queue.Queue = queue.Queue()
    done = threading.Event()
    threading.Thread(target=read, args=(inbox, done), daemon=True).start()
    while not done.wait(cfg.flush_s):
        writer.step(take(inbox))
        say({"stats": writer.stats})
    writer.step(take(inbox), final=True)
    writer.close()
    say({"stats": writer.stats})
    return 0


if __name__ == "__main__":
    sys.exit(main())
