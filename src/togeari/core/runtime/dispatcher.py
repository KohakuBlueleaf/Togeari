"""The dispatcher: the only thread that touches job state (spec/03).

Its stages live in their own modules: fetch (``frontend``), issue and
writeback (``backend``), faults, retire and snapshots (``retire``), the job
pool and admission (``pool``), and the supervised loop (``loop``).
"""

import queue
import random
import threading
import time
from collections import deque
from typing import Any

from togeari.core.runtime.backend.issue import IssueStage
from togeari.core.runtime.backend.writeback import UndeclaredMutation, WritebackStage
from togeari.core.runtime.frontend.fetch import FrontEnd
from togeari.core.runtime.handles import DispatcherDied, JobHandle, Reply, RunState
from togeari.core.runtime.loop import LoopStage
from togeari.core.runtime.pool.jobs import JobsStage
from togeari.core.runtime.retire.faults import FaultStage
from togeari.core.runtime.retire.retire import RetireStage
from togeari.core.runtime.state import Inflight, Job


class Dispatcher(IssueStage, WritebackStage, FaultStage, RetireStage, JobsStage, LoopStage):
    def __init__(self, machine: Any, seed: int = 0):
        self.m = machine
        self.code = machine.code
        self.knobs = machine.knobs
        self.plugins = machine.plugins
        self.bus = machine.bus
        self.store = machine.store
        self.lanes = machine.lanes
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.front = FrontEnd(self)
        self.record_rng = random.Random(seed)
        self.event_rng = random.Random(seed + 1)
        # The scheduling clock (seconds, monotonic): deadlines, timers, batch waits
        self.clock = time.monotonic

        # Job pool and scheduling sets; jobs to fetch for, in the order they became dirty
        self.jobs: dict[int, Job] = {}
        self.keys: set[str] = set()
        self.ready: list[Inflight] = []
        self.dirty: dict[int, None] = {}
        self.last_started: Any = None
        self.blocked: dict[int, list[Inflight]] = {}
        self.pool_bytes = 0
        # (job, bytes) released by freed held slots, from any thread
        self.freed: deque = deque()
        self.warned_keys = False

        # Source and sink of the current run
        self.run: RunState | None = None
        self.source_buf: queue.Queue | None = None
        self.source_want = False
        self.sink_q: queue.Queue | None = None
        self.sink_pending: deque = deque()
        self.sink_wait = False
        self.on_idle = threading.Event()

        # Run state and totals
        self.running = False
        self.stopping = False
        self.dead: BaseException | None = None
        # The check-then-set of ``dead`` (loop thread and abandoning thread)
        self.death_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.inline: dict = {}
        self.stats: dict[str, Any] = {
            "jobs_ok": 0,
            "jobs_failed": 0,
            "calls": 0,
            "squashed": 0,
            "reused": 0,
            "wasted": 0,
            "inline_s": {},
            "store_hits": 0,
            "store_misses": 0,
            "store_weak": 0,
            "store_errors": 0,
            "snapshot_errors": 0,
            "weak_digests": {},
            "machine_errors": 0,
            "records_blocked": 0,
            "records_dropped": 0,
            "cycles": 0,
            "busy_s": 0.0,
            "phase_s": {},
            "cycle_hist": {},
            "cycle_max_s": 0.0,
        }
        self.started_at = 0.0

    def on_thread(self) -> bool:
        return self.thread is not None and threading.current_thread() is self.thread


__all__ = ["Dispatcher", "DispatcherDied", "JobHandle", "Reply", "UndeclaredMutation"]
