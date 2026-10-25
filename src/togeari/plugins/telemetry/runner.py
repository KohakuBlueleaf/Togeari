"""Where the writer runs: ``Local`` (a thread of the machine's process) or ``Child`` (a process
of its own, ``child.py``, reading the ring through a file mapping). Both take the feeder's
messages every ``flush_s`` and answer ``status()``; ``stop()`` sends the last messages, closes
every bucket and the files.
"""

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from togeari.plugins.telemetry.feeder import Feeder
from togeari.plugins.telemetry.writer import KNOBS, TICKS_KEPT, Writer

log = logging.getLogger("togeari.telemetry")

# Seconds the child has to start (imports, files) and to finish at stop
READY_S = 120.0
STOP_S = 120.0
LOG_FILE = "writer.log"
LOG_TAIL = 4000


class Local:
    """The writer on a thread of the machine's process."""

    def __init__(self, cfg: Any, feeder: Feeder, ring: Any, root: Path):
        self.cfg = cfg
        self.feeder = feeder
        self.writer = Writer(cfg, ring, root)
        self.halt = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, initial: list) -> None:
        self.writer.step(initial)
        self.thread = threading.Thread(target=self.loop, name="togeari-telemetry", daemon=True)
        self.thread.start()

    def loop(self) -> None:
        while not self.halt.wait(self.cfg.flush_s):
            self.writer.step(self.feeder.poll())

    def stop(self) -> None:
        if self.halt.is_set():
            return
        self.halt.set()
        if self.thread is not None:
            self.thread.join(timeout=30)
            self.thread = None
        self.writer.step(self.feeder.poll(final=True), final=True)
        self.writer.close()

    def status(self) -> dict:
        w = self.writer
        return {"mode": "thread", **w.stats, "ticks_ms": list(w.ticks_ms)}


class Child:
    """The writer in a process of its own: messages are JSON lines on its stdin, each step's
    stats a JSON line on its stdout, its log ``writer.log`` in the run directory. A child that
    exits is logged and counted; the ring then drops, and the machine never waits."""

    def __init__(self, cfg: Any, feeder: Feeder, ring: Any, root: Path):
        self.cfg = cfg
        self.feeder = feeder
        self.ring = ring
        # Absolute: the child runs in this directory, so a relative one would nest in itself
        self.root = Path(root).resolve()
        self.halt = threading.Event()
        self.ready = threading.Event()
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.reader: threading.Thread | None = None
        self.last: dict = {}
        self.ticks_ms: deque = deque(maxlen=TICKS_KEPT)
        self.lost: str | None = None
        self.sent = 0

    def start(self, initial: list) -> None:
        # The child imports what this process imported, from where it did; its working
        # directory is the run directory, so nothing else shadows a module
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path[1:] if p)}
        config = {
            "ring": str(Path(self.ring.path).resolve()),
            "root": str(self.root),
            "knobs": {k: getattr(self.cfg, k) for k in KNOBS},
        }
        with open(self.root / LOG_FILE, "ab") as logf:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "togeari.plugins.telemetry.child"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=logf,
                cwd=self.root,
                env=env,
            )
        self.reader = threading.Thread(
            target=self.read, name="togeari-telemetry-status", daemon=True
        )
        self.reader.start()
        self.send([{"config": config}, *initial])
        if not self.ready.wait(READY_S) or self.lost is not None:
            self.kill()
            raise RuntimeError(f"the telemetry writer did not start:\n{self.log_tail()}")

        # Mapped by the child now; where the system allows it the name goes at once, so no
        # file outlives the processes
        with contextlib.suppress(OSError):
            os.unlink(self.ring.path)
        self.thread = threading.Thread(target=self.loop, name="togeari-telemetry", daemon=True)
        self.thread.start()

    def loop(self) -> None:
        while not self.halt.wait(self.cfg.flush_s):
            self.send(self.feeder.poll())

    def send(self, messages: list) -> None:
        if self.lost is not None or not messages:
            return
        data = b"".join(json.dumps(m, allow_nan=False).encode() + b"\n" for m in messages)
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            self.sent += len(messages)
        except (OSError, ValueError) as exc:
            self.gone(f"stdin: {exc!r}")

    def read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ready"):
                self.ready.set()
            if "stats" in msg:
                self.last = msg["stats"]
                self.ticks_ms.append(msg["stats"]["tick_ms"])
        code = self.proc.wait()
        if not self.halt.is_set():
            self.gone(f"exit code {code}")
        self.ready.set()

    def gone(self, why: str) -> None:
        if self.lost is None:
            self.lost = why
            log.warning("telemetry: the writer process is gone (%s):\n%s", why, self.log_tail())

    def stop(self) -> None:
        if self.halt.is_set():
            return
        self.halt.set()
        if self.thread is not None:
            self.thread.join(timeout=30)
            self.thread = None
        if self.proc is None:
            return
        self.send([*self.feeder.poll(final=True), {"stop": True}])
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(STOP_S)
        except subprocess.TimeoutExpired:
            log.warning("telemetry: the writer process did not finish in %s s", STOP_S)
            self.kill()
        if self.reader is not None:
            self.reader.join(timeout=10)

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def log_tail(self) -> str:
        try:
            return (self.root / LOG_FILE).read_text(encoding="utf-8", errors="replace")[-LOG_TAIL:]
        except OSError:
            return ""

    def status(self) -> dict:
        p = self.proc
        return {
            "mode": "process",
            **self.last,
            "ticks_ms": list(self.ticks_ms),
            "pid": None if p is None else p.pid,
            "exit": None if p is None else p.poll(),
            "lost": self.lost,
            "sent": self.sent,
        }
