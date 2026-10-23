"""Bounded committed-event outbox: journal commitment and sink delivery are separate.

Only journal append acknowledgement permits retirement. Sink delivery is at least once;
its acknowledgement releases capacity only after journal.ack succeeds. Store/consumer
calls run on two daemon workers, never the dispatcher. A blocked backend cannot be
preempted: shutdown reports unknown appends and durable pending events explicitly.
"""

import copy
import heapq
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from togeari.core.contracts.events import CommittedEvent, EventConflict, EventOutput, EventTooLarge

log = logging.getLogger("togeari.events")


@dataclass
class _Entry:
    event: CommittedEvent
    token: dict
    wake: Any = None
    retired: bool = False
    delivery: str = "append"


class EventOutboxManager:
    """One machine's output channel; callbacks and core-visible tokens settle on dispatcher."""

    def __init__(self, machine, output: EventOutput | None = None):
        self.m = machine
        self.output = output
        self._identity = None if output is None else output.descriptor()["journal"]
        self._entries: dict[str, _Entry] = {}
        self._bytes = 0
        self._waiters: dict[str, Any] = {}
        self._frontiers: dict[str, int] = {}
        self._errors: dict[str, str] = {}
        self._restores: dict[int, Any] = {}
        self._restore_accepting = True
        self.pending_restores = 0
        self._committed = self._delivered = 0
        self._callback_errors = 0
        self._recovering = output is not None
        self._recovery_error = None
        self._started = False
        self._accepting = True
        self._wake_enabled = True
        self._closing = False
        self._closed = False
        self._sequence = itertools.count()
        self._condition = threading.Condition()
        self._journal_tasks = []
        self._sink_tasks = queue.Queue(maxsize=output.max_events if output else 1)
        self._completions = queue.Queue()
        self._drain_lock = threading.RLock()
        self._threads = []
        self._close_report = None

    def configuration(self) -> dict:
        if self.output is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "journal_identity": copy.deepcopy(self._identity),
            "max_events": self.output.max_events,
            "max_bytes": self.output.max_bytes,
            "retry_s": self.output.retry_s,
            "close_timeout_s": self.output.close_timeout_s,
            "delivery": "at-least-once",
        }

    def start(self) -> None:
        """Recover journal backlog even when no new emitting job is submitted."""
        if self.output is None or self._started or self._closed:
            return
        self._started = True
        self._threads = [
            threading.Thread(
                target=self._journal_worker, name="togeari-event-journal", daemon=True
            ),
            threading.Thread(target=self._sink_worker, name="togeari-event-sink", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        self._schedule("recover", None)

    def prepare(self, jobuid, sequence, invocation_id, kir_event, origin=None) -> CommittedEvent:
        if self.output is None:
            raise ValueError("semantic emit requires event_output")
        payload = kir_event.to_dict()
        event = CommittedEvent(
            jobuid, sequence, invocation_id, payload["topic"], payload["payload"], origin
        )
        if event.size_bytes > self.output.max_bytes:
            raise EventTooLarge(
                f"event is {event.size_bytes} bytes; outbox limit is {self.output.max_bytes}"
            )
        return event

    # Accepted entries consume both limits through durable sink acknowledgement.
    def commit(self, event: CommittedEvent, wake) -> dict:
        if self.output is None:
            return {"status": "failed", "error": "semantic emit requires event_output"}
        if type(event) is not CommittedEvent:
            raise TypeError("outbox accepts an exact CommittedEvent")
        existing = self._entries.get(event.event_id)
        if existing is not None:
            if existing.event.semantic_bytes() != event.semantic_bytes():
                raise EventConflict("event ID reused with a different envelope")
            return existing.token
        if event.size_bytes > self.output.max_bytes:
            raise EventTooLarge("event exceeds configured outbox byte limit")
        if not self._accepting or self._closed:
            return {"status": "failed", "event_id": event.event_id, "error": "event output closed"}
        self.start()
        if self._recovery_error is not None:
            return {"status": "failed", "event_id": event.event_id, "error": self._recovery_error}
        if (
            self._recovering
            or len(self._entries) >= self.output.max_events
            or self._bytes + event.size_bytes > self.output.max_bytes
        ):
            self._waiters[event.job_execution_id] = wake
            return {"status": "full", "event_id": event.event_id}
        token = {"status": "pending", "event_id": event.event_id}
        self._entries[event.event_id] = _Entry(event, token, wake)
        self._bytes += event.size_bytes
        self._schedule("append", event)
        return token

    def retired(self, event: CommittedEvent) -> None:
        entry = self._entries.get(event.event_id)
        if entry is None:
            return
        if entry.token["status"] != "committed":
            raise RuntimeError("event cannot retire before journal append acknowledgement")
        entry.retired = True
        self._deliver_next(event.job_execution_id)

    def pending(self, jobuid) -> bool:
        """An accepted append has not resolved; cancellation must not squash its owner."""
        return any(
            entry.event.job_execution_id == jobuid and entry.token["status"] == "pending"
            for entry in self._entries.values()
        )

    def status(self, jobuid=None) -> dict:
        entries = [
            entry
            for entry in self._entries.values()
            if jobuid is None or entry.event.job_execution_id == jobuid
        ]
        result = {
            "enabled": self.output is not None,
            "journal_identity": copy.deepcopy(self._identity),
            "pending_events": len(entries),
            "pending_bytes": sum(entry.event.size_bytes for entry in entries),
            "append_pending": sum(entry.token["status"] == "pending" for entry in entries),
            "durable_pending": [
                entry.event.event_id for entry in entries if entry.token["status"] == "committed"
            ],
            "recovering": self._recovering,
            "recovery_error": self._recovery_error,
            "closed": self._closed,
            "callback_errors": self._callback_errors,
        }
        if jobuid is None:
            result.update(committed=self._committed, delivered=self._delivered)
        else:
            result.update(
                job_execution_id=jobuid,
                committed_watermark=self._frontiers.get(jobuid, -1),
                delivery_error=self._errors.get(jobuid),
            )
        return result

    def check_frontier(self, jobuid, event_state, callback) -> None:
        """Async restore preflight; callback receives allow/issues before any node executes."""
        self.start()
        if self.output is None or self._closed or not self._restore_accepting:
            callback(self._denied("event_output_missing", "event output is unavailable"))
            return
        try:
            if type(event_state) is not dict or set(event_state) != {
                "version",
                "next_sequence",
                "journal_identity",
                "committed_watermark",
            }:
                raise ValueError("invalid snapshot event_state fields")
            watermark = event_state["committed_watermark"]
            next_sequence = event_state["next_sequence"]
            if (
                type(event_state["version"]) is not int
                or event_state["version"] != 1
                or type(watermark) is not int
                or watermark < -1
                or type(next_sequence) is not int
                or next_sequence != watermark + 1
                or event_state["journal_identity"] != self._identity
            ):
                raise ValueError("snapshot event journal identity or sequence does not match")
        except ValueError as exc:
            callback(self._denied("event_state_incompatible", str(exc)))
            return
        if self.pending_restores >= self.output.max_events:
            callback(
                self._denied("event_preflight_full", "event frontier preflight capacity exhausted")
            )
            return
        token = next(self._sequence)
        self._restores[token] = callback
        self.pending_restores += 1
        self._schedule("frontier", (token, jobuid, watermark))

    def cancel_restores(self):
        """Resolve pending admissions on dispatcher before stop; do not cancel any append."""
        self._restore_accepting = False
        pending, self._restores = self._restores, {}
        for callback in pending.values():
            try:
                self._invoke(
                    callback,
                    self._denied("event_output_stopping", "machine stopped during frontier read"),
                )
            finally:
                self.pending_restores -= 1

    @staticmethod
    def _denied(code, message):
        return {
            "allow": False,
            "status": "incompatible",
            "issues": [{"code": code, "path": "event_state", "message": message}],
        }

    # Journal and sink workers own no scheduler objects and communicate only by completions.
    def _schedule(self, kind, value, delay=0.0):
        with self._condition:
            if self._closed:
                return
            heapq.heappush(
                self._journal_tasks, (time.monotonic() + delay, next(self._sequence), kind, value)
            )
            self._condition.notify()

    def _complete(self, kind, value, result=None, error=None):
        if self._closed:
            return
        if error is not None:
            error = error.with_traceback(None)
        self._completions.put((kind, value, result, error))
        with self._condition:
            self._condition.notify_all()
        if not self._closed and not self._closing:
            self.m.dispatcher.inbox.put(("call", self._drain))

    def _journal_worker(self):
        while True:
            with self._condition:
                while not self._closed:
                    if not self._journal_tasks:
                        self._condition.wait()
                        continue
                    delay = self._journal_tasks[0][0] - time.monotonic()
                    if delay > 0:
                        self._condition.wait(delay)
                        continue
                    _, _, kind, value = heapq.heappop(self._journal_tasks)
                    break
                if self._closed:
                    return
            try:
                journal = self.output.journal
                match kind:
                    case "append":
                        result = journal.append(value)
                    case "ack":
                        result = journal.ack(value.event_id)
                    case "recover":
                        result = journal.pending(
                            limit=self.output.max_events, max_bytes=self.output.max_bytes
                        )
                    case "frontier":
                        result = journal.frontier(value[1])
                    case "retry_sink":
                        self._complete(kind, value)
                        continue
                    case _:
                        raise RuntimeError(f"unknown event journal task {kind}")
                self._complete(kind, value, result)
            except Exception as exc:  # noqa: BLE001
                self._complete(kind, value, error=exc)
            value = result = None

    def _sink_worker(self):
        while True:
            event = self._sink_tasks.get()
            if event is None:
                return
            try:
                if event is _CLOSE:
                    self.output.sink.close()
                    self._complete("sink_close", None)
                else:
                    self.output.sink.write_event(event)
                    self._complete("sink", event)
            except Exception as exc:  # noqa: BLE001
                self._complete("sink_close" if event is _CLOSE else "sink", event, error=exc)
            event = None

    def _drain(self):
        with self._drain_lock:
            while True:
                try:
                    kind, value, result, error = self._completions.get_nowait()
                except queue.Empty:
                    break
                self._acknowledge(kind, value, result, error)

    def _invoke(self, callback, *args):
        try:
            callback(*args)
        except Exception:
            self._callback_errors += 1
            log.exception("event output core callback failed")

    def _notify(self, callback):
        if callback is not None and self._wake_enabled and not self._closing and not self._closed:
            self._invoke(callback)

    def _wake_capacity(self):
        waiters, self._waiters = self._waiters, {}
        for callback in waiters.values():
            self._notify(callback)

    def _release(self, entry):
        self._entries.pop(entry.event.event_id, None)
        self._bytes -= entry.event.size_bytes
        self._wake_capacity()

    def _acknowledge(self, kind, value, result, error):
        if kind == "frontier":
            token, jobuid, expected = value
            callback = self._restores.pop(token, None)
            if callback is None:
                return
            decision = {"allow": True, "status": "compatible", "issues": []}
            if error is not None:
                decision = self._denied("event_journal_read", str(error))
            elif type(result) is not int or result != expected:
                decision = self._denied(
                    "event_frontier_mismatch",
                    f"snapshot frontier {expected}, journal frontier {result}",
                )
            else:
                self._frontiers[jobuid] = result
            try:
                self._invoke(callback, decision)
            finally:
                self.pending_restores -= 1
            return
        if kind == "sink_close":
            if self._close_report is not None:
                self._close_report[kind] = "failed" if error is not None else "closed"
                if error is not None:
                    self._close_report[kind + "_error"] = str(error)
            return
        if kind == "recover":
            if error is not None:
                self._recovery_error = f"{type(error).__name__}: {error}"
                self._recovering = False
                self._wake_capacity()
                return
            if type(result) is not list:
                self._recovery_error = "journal pending must return an exact bounded list"
                self._recovering = False
                self._wake_capacity()
                return
            events = result
            if (
                len({event.event_id for event in events if type(event) is CommittedEvent})
                != len(events)
                or len(events) > self.output.max_events
                or any(type(event) is not CommittedEvent for event in events)
                or sum(event.size_bytes for event in events) > self.output.max_bytes
            ):
                self._recovery_error = "journal returned an oversized/invalid recovery page"
                self._recovering = False
                self._wake_capacity()
                return
            if not events:
                self._recovering = False
                self._wake_capacity()
                return
            for event in events:
                token = {"status": "committed", "event_id": event.event_id}
                self._entries[event.event_id] = _Entry(event, token, retired=True, delivery="ready")
                self._bytes += event.size_bytes
                self._frontiers[event.job_execution_id] = max(
                    self._frontiers.get(event.job_execution_id, -1), event.sequence
                )
            for jobuid in {event.job_execution_id for event in events}:
                self._deliver_next(jobuid)
            return
        entry = self._entries.get(value.event_id)
        if entry is None:
            return
        jobuid = entry.event.job_execution_id
        if entry.token["status"] == "unknown":
            # Shutdown already returned an unknown terminal outcome. A late journal reply
            # is diagnostic only: never resurrect the core owner or deliver before recovery.
            entry.token["late_persistence"] = "committed" if error is None else "unknown"
            if error is not None:
                entry.token["late_error"] = f"{type(error).__name__}: {error}"
            entry.wake = None
            return
        if error is not None:
            entry.token["error"] = f"{type(error).__name__}: {error}"
            self._errors[jobuid] = entry.token["error"]
            if kind == "append" and isinstance(error, EventConflict):
                entry.token["status"] = "failed"
                self._release(entry)
                self._notify(entry.wake)
                return
            retry_kind = "retry_sink" if kind == "sink" else kind
            self._schedule(retry_kind, value, self.output.retry_s)
            return
        match kind:
            case "append":
                entry.token["status"] = "committed"
                entry.token.pop("error", None)
                entry.delivery = "ready"
                self._committed += 1
                self._frontiers[jobuid] = max(self._frontiers.get(jobuid, -1), value.sequence)
                self._notify(entry.wake)
            case "sink":
                entry.delivery = "ack"
                self._schedule("ack", value)
            case "ack":
                self._delivered += 1
                self._errors.pop(jobuid, None)
                self._release(entry)
                self._deliver_next(jobuid)
                if self._recovering and not self._entries:
                    self._schedule("recover", None)
            case "retry_sink":
                self._sink_tasks.put_nowait(value)

    def _deliver_next(self, jobuid):
        entries = sorted(
            (entry for entry in self._entries.values() if entry.event.job_execution_id == jobuid),
            key=lambda entry: entry.event.sequence,
        )
        if not entries:
            return
        first = entries[0]
        if first.retired and first.token["status"] == "committed" and first.delivery == "ready":
            first.delivery = "sink"
            self._sink_tasks.put_nowait(first.event)

    def force_unknown(self) -> list[dict]:
        """Dispatcher shutdown escape: report unresolved appends, never cancel backend writes."""
        self._accepting = False
        self._wake_enabled = False
        self._waiters.clear()
        unknown = []
        for entry in self._entries.values():
            if entry.token["status"] == "pending":
                entry.token["status"] = "unknown"
                unknown.append(
                    {
                        "job_execution_id": entry.event.job_execution_id,
                        "event_id": entry.event.event_id,
                    }
                )
        return unknown

    def close(self, timeout=None) -> dict:
        """Bounded drain after dispatcher stops; unknown append != absent event."""
        if self._closed:
            return copy.deepcopy(self._close_report or self.status())
        self._accepting = False
        self._closing = True
        self._wake_enabled = False
        cancelled_restores = len(self._restores)
        self._restores.clear()  # close runs after core shutdown, including queued read completions
        self.pending_restores = 0
        if self.output is None or not self._started:
            self._closed = True
            self._close_report = {"status": "closed", "unknown_appends": [], "durable_pending": []}
            return copy.deepcopy(self._close_report)
        deadline = time.monotonic() + (
            self.output.close_timeout_s if timeout is None else max(0.0, timeout)
        )
        self._close_report = {}
        close_submitted = False
        while True:
            self._drain()
            if not self._entries and not self.pending_restores and not self._recovering:
                if not close_submitted:
                    close_submitted = True
                    self._sink_tasks.put_nowait(_CLOSE)
                if "sink_close" in self._close_report:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self._condition:
                if self._completions.empty():
                    self._condition.wait(remaining)
        unknown = [
            entry.event.event_id
            for entry in self._entries.values()
            if entry.token["status"] in ("pending", "unknown")
        ]
        durable = [
            entry.event.event_id
            for entry in self._entries.values()
            if entry.token["status"] == "committed"
        ]
        self._close_report.update(
            status=(
                "closed"
                if not unknown
                and not durable
                and not self._recovering
                and not self.pending_restores
                and self._recovery_error is None
                and self._close_report.get("sink_close") == "closed"
                else "pending"
            ),
            unknown_appends=unknown,
            durable_pending=durable,
            recovery_error=self._recovery_error,
            pending_restores=self.pending_restores,
            journal="retained",
        )
        for entry in self._entries.values():
            if entry.token["status"] == "pending":
                entry.token["status"] = "unknown"
            entry.wake = None
        self._waiters.clear()
        self._close_report["cancelled_restores"] = cancelled_restores
        self._closed = True
        with self._condition:
            self._journal_tasks.clear()
            self._condition.notify_all()
        while True:
            try:
                self._sink_tasks.get_nowait()
            except queue.Empty:
                break
        self._sink_tasks.put_nowait(None)
        return copy.deepcopy(self._close_report)


_CLOSE = object()
