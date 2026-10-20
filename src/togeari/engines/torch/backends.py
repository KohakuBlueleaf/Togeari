"""Where a placement's runtime lives: in this process (``LocalDevice``) or a child
(``RemoteDevice``).

A placement is one device or a tuple of devices (``runtime.placement``); ``device`` is its
name (``runtime.label``). Both take ``Job``s through ``put`` without blocking and answer each
through ``job.reply`` exactly once; ``alive`` is False once the placement cannot run.
"""

import contextlib
import heapq
import itertools
import multiprocessing
import queue
import threading
import time
from typing import Any

from togeari.engines.common.errors import EngineStopped, WorkerDied
from togeari.engines.common.transport import SHM_THRESHOLD, Packet, release, unpack_error
from togeari.engines.torch import child
from togeari.engines.torch.codec import tensor_codec
from togeari.engines.torch.runtime import SKIPPED, DeviceRuntime, Job, label, placement

_SPAWN = multiprocessing.get_context("spawn")


class LocalDevice:
    """The runtime on threads of the machine's process."""

    def __init__(self, device: str | tuple[str, ...], models: dict, runtime: dict):
        self.device = label(placement(device))
        self.runtime = DeviceRuntime(device, models, **runtime)
        self.alive = False

    def start(self) -> dict:
        footprint = self.runtime.start()
        self.alive = True
        return footprint

    def put(self, job: Job) -> None:
        self.runtime.put(job)

    def cancel(self, job: Job) -> None:
        """A job not yet running is skipped; a running one cannot be stopped."""
        job.cancelled = True

    def revive(self) -> None:
        """Nothing to revive: the runtime lives as long as the device is started."""

    def stop(self) -> None:
        self.alive = False
        self.runtime.stop()

    def stats(self) -> dict:
        return {**self.runtime.stats(), "restarts": 0, "kills": 0, "lost": False}


class RemoteDevice:
    """The runtime in a spawned process; payloads and results cross through the transport.

    A sender thread encodes and sends jobs; a reader thread receives replies and
    answers jobs. When the process dies, its in-flight jobs fail with
    ``WorkerDied`` and the process restarts with every model reloaded; when it
    cannot restart, the device is lost: every job fails with ``EngineStopped``
    and ``revive()`` starts one respawn at a time, no sooner than the backoff
    after the last failure (``revive_backoff_s``: first and longest wait).

    A cancelled job the process has not answered ``cancel_kill_s`` seconds after
    its cancel gets the process killed (counted in ``kills``), which then
    restarts as above; None never kills.
    """

    def __init__(
        self,
        device: str | tuple[str, ...],
        models: dict,
        runtime: dict,
        shm: bool = True,
        threshold: int = SHM_THRESHOLD,
        start_timeout_s: float = 300.0,
        cancel_kill_s: float | None = 5.0,
        revive_backoff_s: tuple[float, float] = (1.0, 60.0),
    ):
        # The placement the child runs, and its name
        self.placement = placement(device)
        self.device = label(self.placement)
        self.models = models
        self.config = {"runtime": runtime, "shm": shm, "threshold": threshold}
        self.codec = tensor_codec(shm=shm, threshold=threshold)
        self.start_timeout_s = start_timeout_s
        self.cancel_kill_s = cancel_kill_s
        self.backoff_min_s, self.backoff_max_s = revive_backoff_s

        # ``io`` guards the pipes, the process, ``jobs`` (id -> job, input blocks) and
        # the lost / reviving state.
        self.io = threading.RLock()
        self.jobs: dict[int, tuple[Job, list]] = {}
        self.outbox: queue.SimpleQueue = queue.SimpleQueue()
        self.proc: Any = None
        self.req: Any = None
        self.rep: Any = None
        self.alive = False
        self.stopping = False
        self.lost = False
        self.reviving = False
        self.backoff_s = self.backoff_min_s
        self.retry_at = 0.0
        self.footprint: dict = {}
        self.remote: dict = {}
        self.restarts = 0
        self.kills = 0
        self.threads: list[threading.Thread] = []
        # Called after a restarted process is installed
        self.on_revived: Any = None

        # Cancelled jobs by kill deadline, guarded by ``watch``.
        self.watch = threading.Condition()
        self.overdue: list[tuple[float, int, Job]] = []
        self.seq = itertools.count()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> dict:
        self._install(*self._spawn())
        self._thread(self._send_loop, "send")
        self._thread(self._read_loop, "read")
        if self.cancel_kill_s is not None:
            self._thread(self._watch_loop, "watch")
        return self.footprint

    def _thread(self, target: Any, role: str) -> None:
        thread = threading.Thread(target=target, name=f"torch-{self.device}-{role}", daemon=True)
        thread.start()
        self.threads.append(thread)

    def _install(self, proc: Any, req: Any, rep: Any, footprint: dict) -> bool:
        """Make a started process the device's; False (and the process ends) when stopping."""
        with self.io:
            if not self.stopping:
                self.proc, self.req, self.rep = proc, req, rep
                self.footprint = footprint
                self.alive = True
                return True
        _end(proc, 0.0)
        return False

    def _spawn(self) -> tuple[Any, Any, Any, dict]:
        """Start a process and wait for its footprint; raises when it fails to load."""
        req_out, req_in = _SPAWN.Pipe(duplex=False)
        rep_out, rep_in = _SPAWN.Pipe(duplex=False)
        proc = _SPAWN.Process(
            target=child.main,
            args=(req_out, rep_in, self.placement, self.models, self.config),
            name=f"torch-{self.device}",
            daemon=True,
        )
        proc.start()
        req_out.close()
        rep_in.close()

        try:
            if not rep_out.poll(self.start_timeout_s):
                raise TimeoutError(f"device {self.device}: no answer in {self.start_timeout_s}s")
            msg = rep_out.recv()
        except (EOFError, OSError, TimeoutError) as exc:
            _end(proc, 0.0)
            raise RuntimeError(f"device {self.device}: process failed to start") from exc
        match msg:
            case ("ready", dict() as footprint):
                return proc, req_in, rep_out, footprint
            case ("failed", bytes() as data):
                _end(proc, 5.0)
                raise unpack_error(data)
            case _:
                _end(proc, 0.0)
                raise RuntimeError(f"device {self.device}: unexpected message {msg!r}")

    def stop(self) -> None:
        self.stopping = True
        self.outbox.put(None)
        with self.watch:
            self.watch.notify_all()
        with self.io:
            self._send(("stop",))
        if self.proc is not None:
            _end(self.proc, 10.0)
        for thread in list(self.threads):
            thread.join(5.0)

        # Jobs the process did not answer fail now.
        with self.io:
            left = list(self.jobs.values())
            self.jobs.clear()
            self.alive = False
        for job, owned in left:
            release(owned, unlink=True)
            job.reply(job, EngineStopped(f"device {self.device} stopped"))

    # ------------------------------------------------------------ jobs
    def put(self, job: Job) -> None:
        self.outbox.put(job)

    def cancel(self, job: Job) -> None:
        job.cancelled = True
        self.outbox.put(("cancel", job.id))
        if self.cancel_kill_s is None:
            return
        with self.watch:
            due = time.monotonic() + self.cancel_kill_s
            heapq.heappush(self.overdue, (due, next(self.seq), job))
            self.watch.notify()

    def _send(self, msg: tuple) -> None:
        """Send under ``io``; a broken pipe is left for the reader to find."""
        with contextlib.suppress(EOFError, OSError):
            self.req.send(msg)

    def _send_loop(self) -> None:
        while (entry := self.outbox.get()) is not None:
            match entry:
                case Job() as job:
                    self._dispatch(job)
                case tuple() as msg:
                    with self.io:
                        self._send(msg)

    def _dispatch(self, job: Job) -> None:
        if job.cancelled:
            job.reply(job, SKIPPED)
            return

        # Payloads are built and encoded here, off the dispatcher.
        try:
            packet, owned = self.codec.encode(job.payloads())
        except Exception as exc:  # noqa: BLE001
            job.reply(job, exc)
            return

        with self.io:
            if not self.alive:
                release(owned, unlink=True)
                job.reply(job, EngineStopped(f"device {self.device} lost"))
                return
            self.jobs[job.id] = (job, owned)
            self._send(("run", job.id, job.model, job.n, packet))

    def _read_loop(self) -> None:
        while True:
            try:
                msg = self.rep.recv()
            except (EOFError, OSError):
                if self.stopping or not self._died():
                    return
                continue

            match msg:
                case ("done", int() as job_id, "ok", Packet() as packet, dict() as stats):
                    try:
                        outcome, _ = self.codec.decode(packet, copy=True)
                    except Exception as exc:  # noqa: BLE001
                        outcome = exc
                    if packet.blocks:
                        with self.io:
                            self._send(("release", job_id))
                case ("done", int() as job_id, "fail", bytes() as data, dict() as stats):
                    outcome = unpack_error(data)
                case ("done", int() as job_id, "skip", None, dict() as stats):
                    outcome = SKIPPED
                case _:
                    continue
            self.remote = stats

            with self.io:
                entry = self.jobs.pop(job_id, None)
            if entry is not None:
                job, owned = entry
                release(owned, unlink=True)
                job.reply(job, outcome)

    # ------------------------------------------------------------ cancel kills
    def _watch_loop(self) -> None:
        """Kill the process under each cancelled job still unanswered at its deadline."""
        while True:
            with self.watch:
                while not self.stopping:
                    wait = self.overdue[0][0] - time.monotonic() if self.overdue else None
                    if wait is not None and wait <= 0:
                        break
                    self.watch.wait(wait)
                if self.stopping:
                    return
                _, _, job = heapq.heappop(self.overdue)
            self._kill_under(job)

    def _kill_under(self, job: Job) -> None:
        """Kill the process when ``job`` is still in it; the reader then restarts it."""
        with self.io:
            entry = self.jobs.get(job.id)
            if entry is None or entry[0] is not job or not self.alive:
                return
            proc = self.proc
            self.kills += 1
        proc.kill()

    # ------------------------------------------------------------ death and revival
    def _died(self) -> bool:
        """Fail the in-flight jobs and restart the process; False when the device is lost.

        The device is not alive while the process restarts: new jobs fail with
        ``EngineStopped`` at once, and ``io`` is free for the sender.
        """
        with self.io:
            proc = self.proc
            code = proc.exitcode if proc is not None else None
            lost = list(self.jobs.values())
            self.jobs.clear()
            self.alive = False
        for job, owned in lost:
            release(owned, unlink=True)
            job.reply(job, WorkerDied(f"device {self.device}: process exited ({code})"))
        if self.stopping:
            return False

        self.restarts += 1
        _end(proc, 0.0)
        return self._respawn()

    def _respawn(self) -> bool:
        """Start and install a new process; on a failure the device is lost until a revive."""
        try:
            started = self._spawn()
        except Exception:  # noqa: BLE001
            with self.io:
                self.lost = True
                self.retry_at = time.monotonic() + self.backoff_s
                self.backoff_s = min(self.backoff_s * 2.0, self.backoff_max_s)
            return False
        if not self._install(*started):
            return False
        with self.io:
            self.lost = False
            self.backoff_s = self.backoff_min_s
        if self.on_revived is not None:
            self.on_revived()
        return True

    def revive(self) -> None:
        """Start a respawn of a lost device in the background, once its backoff has passed."""
        with self.io:
            due = time.monotonic() >= self.retry_at
            if not self.lost or self.reviving or self.stopping or not due:
                return
            self.reviving = True
        threading.Thread(
            target=self._revive, name=f"torch-{self.device}-revive", daemon=True
        ).start()

    def _revive(self) -> None:
        try:
            if self._respawn():
                self._thread(self._read_loop, "read")
        finally:
            with self.io:
                self.reviving = False

    def stats(self) -> dict:
        return {**self.remote, "restarts": self.restarts, "kills": self.kills, "lost": self.lost}


def _end(proc: Any, grace_s: float) -> None:
    """Wait up to ``grace_s`` for the process to exit, then kill it."""
    deadline = time.monotonic() + grace_s
    while proc.is_alive() and time.monotonic() < deadline:
        proc.join(0.05)
    if proc.is_alive():
        proc.kill()
    proc.join(5.0)
