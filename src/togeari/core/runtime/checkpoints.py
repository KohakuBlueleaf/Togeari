"""Pinned requested checkpoints and explicit resume, with bounded off-dispatcher store IO.

Cut deadlines use the dispatcher's scheduling clock; IO deadlines use real monotonic
seconds. A timed-out write that has entered the store has an unknown outcome, not a
promise that no artifact exists. Pickle artifacts, like snapshots, require trusted stores.
"""

import copy
import heapq
import itertools
import math
import pickle
import queue
import re
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from togeari.core.contracts.digest import digest, digest_with_weak
from togeari.core.contracts.io import MISS, JobRecord, SourceItem
from togeari.core.program.execution import observe_execution
from togeari.core.runtime.retire.codec import prepare_restore

IO_WORKERS = 2
IO_QUEUE_SIZE = 64
HISTORY_LIMIT = 1024
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_STEP_BUDGET = 100_000
SCHEMA = "togeari.checkpoint"
VERSION = 1
_TOKEN = re.compile(r"[0-9a-f]{32}\Z")


def checkpoint_key(checkpoint_id: str) -> bytes:
    """A pinned artifact never shares an automatic snapshot locator."""
    if type(checkpoint_id) is not str or not _TOKEN.fullmatch(checkpoint_id):
        raise ValueError("checkpoint_id must be 32 lowercase hexadecimal characters")
    return digest((SCHEMA, VERSION, checkpoint_id))


class CheckpointResumeError(ValueError):
    """An explicit artifact cannot be admitted; never permission to start a fresh job."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass
class _Work:
    run: Any
    acknowledge: Any
    entered: bool = False
    cancelled: bool = False
    lock: Any = field(default_factory=threading.Lock)

    def cancel(self) -> bool:
        """Cancel a queued operation; return whether it already entered its store call."""
        with self.lock:
            self.cancelled = True
            if not self.entered:
                self.run = self.acknowledge = None
            return self.entered


class CheckpointManager:
    """Dispatcher-owned requests; workers touch immutable payloads and operation locks only."""

    def __init__(self, machine):
        self.m = machine
        self._requests: dict[str, dict] = {}
        self._resumes: dict[int, dict] = {}
        self._staged: dict[int, dict] = {}
        self._deletes: dict[str, tuple] = {}
        self.pending_resumes = 0
        self.persisted = 0
        self._queue = queue.Queue(maxsize=IO_QUEUE_SIZE)
        self._condition = threading.Condition()
        self._timers = []
        self._sequence = itertools.count()
        self._threads = []
        self._started = False
        self._closed = False

    # Workers never inspect jobs, perform admission, or mutate request dictionaries.
    def _start(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("checkpoint manager is stopped")
            if self._started:
                return
            self._started = True
            self._threads = [
                threading.Thread(
                    target=self._worker, name=f"togeari-checkpoint-io-{index}", daemon=True
                )
                for index in range(IO_WORKERS)
            ]
            self._threads.append(
                threading.Thread(
                    target=self._timer, name="togeari-checkpoint-deadlines", daemon=True
                )
            )
            for thread in self._threads:
                thread.start()

    def _post(self, callback):
        if not self._closed:
            self.m.dispatcher.inbox.put(("call", callback))

    def _worker(self):
        while True:
            work = self._queue.get()
            if work is None:
                return
            with work.lock:
                if work.cancelled or self._closed:
                    continue
                work.entered = True
                run, work.run = work.run, None
            try:
                value, error = run(), None
            except BaseException as exc:  # noqa: BLE001
                value, error = None, f"{type(exc).__name__}: {exc}"
            finally:
                run = None  # release immutable artifact bytes before waiting for another task
            acknowledge, work.acknowledge = work.acknowledge, None
            self._post(lambda fn=acknowledge, v=value, e=error: fn(v, e))
            work = acknowledge = value = error = None

    def _timer(self):
        while True:
            with self._condition:
                while not self._closed:
                    if not self._timers:
                        self._condition.wait()
                    else:
                        delay = self._timers[0][0] - time.monotonic()
                        if delay > 0:
                            self._condition.wait(delay)
                        else:
                            _, _, callback = heapq.heappop(self._timers)
                            break
                if self._closed:
                    return
            self._post(callback)

    def _schedule(self, deadline, callback):
        self._start()
        with self._condition:
            heapq.heappush(self._timers, (deadline, next(self._sequence), callback))
            self._condition.notify()

    def _enqueue(self, work):
        self._start()
        try:
            self._queue.put_nowait(work)
            return True
        except queue.Full:
            return False

    @staticmethod
    def _limits(timeout_s, step_budget):
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s < 0
        ):
            raise ValueError("timeout_s must be finite and nonnegative")
        if type(step_budget) is not int or step_budget < 0:
            raise ValueError("step_budget must be a nonnegative integer")

    # A request token is allocated before any cut or store work starts.
    def request(self, jobid, timeout_s=DEFAULT_TIMEOUT_S, step_budget=DEFAULT_STEP_BUDGET):
        self._limits(timeout_s, step_budget)
        token = uuid.uuid4().hex
        future = Future()
        row = {
            "checkpoint_id": token,
            "job": jobid,
            "status": "requested",
            "future": future,
            "work": None,
            "deadline": time.monotonic() + timeout_s,
        }
        self._requests[token] = row
        if self._closed or self.m.store is None:
            self.finished(token, "failed", {"reason": "stopped" if self._closed else "no_store"})
            return future
        if timeout_s == 0:
            self.finished(token, "timeout", {})
            return future
        self._schedule(row["deadline"], lambda: self._timeout(token))
        try:
            reply = self.m.dispatcher.begin_checkpoint(
                jobid, token, self.m.dispatcher.clock() + timeout_s, step_budget
            )
        except Exception as exc:  # noqa: BLE001
            self.finished(token, "failed", {"reason": str(exc)})
            return future
        if not future.done():
            if reply.get("status") == "pending":
                if row["status"] == "requested":
                    row["status"] = "draining"
            else:
                self.finished(token, reply.get("status", "failed"), reply)
        return future

    def _public(self, row):
        return copy.deepcopy(
            {key: value for key, value in row.items() if key not in {"future", "work", "deadline"}}
        )

    def query(self, job=None, checkpoint_id=None):
        """Detached progress, including late persistence acknowledgements."""
        return [
            self._public(row)
            for token, row in self._requests.items()
            if (job is None or row["job"] == job)
            and (checkpoint_id is None or token == checkpoint_id)
        ]

    def _prune_history(self):
        excess = len(self._requests) - HISTORY_LIMIT
        for token, row in list(self._requests.items()):
            if excess <= 0:
                break
            if row["future"].done() and row["work"] is None:
                del self._requests[token]
                excess -= 1

    def finished(self, requestid, status, details=None):
        row = self._requests.get(requestid)
        if row is None or row["future"].done():
            return
        row.update(
            {
                k: v
                for k, v in (details or {}).items()
                if k not in {"future", "work", "deadline", "checkpoint_id", "job", "status"}
            }
        )
        row["status"] = status
        work = row["work"]
        if work is not None and not work.entered and not work.cancel():
            row["work"] = None
        row["future"].set_result(self._public(row))
        self._requests.pop(requestid)
        self._requests[requestid] = row
        self._prune_history()

    def _cancel_cut(self, token):
        self.m.dispatcher.cancel_checkpoint(token)

    def _timeout(self, token):
        row = self._requests.get(token)
        if row is None or row["future"].done():
            return
        work = row["work"]
        entered = work.cancel() if work is not None else False
        # Settle first: a core cancellation callback must not overwrite timeout semantics.
        self.finished(token, "persistence_unknown" if entered else "timeout", {})
        self._cancel_cut(token)

    def cancel(self, checkpoint_id):
        row = self._requests.get(checkpoint_id)
        if row is None or row["future"].done():
            return False
        work = row["work"]
        entered = work.cancel() if work is not None else False
        self.finished(checkpoint_id, "persistence_unknown" if entered else "cancelled", {})
        self._cancel_cut(checkpoint_id)
        return True

    def captured(self, requestid, blob, cutinfo):
        """Core callback. Identity metadata comes from admission, never mutated job.inputs."""
        row = self._requests.get(requestid)
        if row is None or row["future"].done() or row["work"] is not None:
            return
        if time.monotonic() >= row["deadline"]:
            self._timeout(requestid)
            return
        try:
            metadata = dict(cutinfo)
            job_key = metadata["job_key"]
            entry = metadata["entry"]
            program_hash = metadata["program_hash"]
            inputs_digest = metadata["inputs_digest"]
            if type(blob) is not bytes or type(job_key) is not str or type(entry) is not str:
                raise ValueError("checkpoint requires snapshot bytes and original key/entry")
            if type(inputs_digest) is not bytes or len(inputs_digest) != 32:
                raise ValueError("checkpoint requires a strong original admission input digest")
            if type(program_hash) is not str:
                raise ValueError("checkpoint requires its semantic program hash")
            saved = pickle.loads(blob)
            if saved.get("version") != 3:
                raise ValueError("checkpoint requires snapshot v3")
            uid = saved["state"]["job_execution_id"]
            if metadata.get("job_execution_id", uid) != uid:
                raise ValueError("checkpoint UID does not match captured snapshot")
            envelope = {
                "schema": SCHEMA,
                "version": VERSION,
                "checkpoint_id": requestid,
                "job_key": job_key,
                "entry": entry,
                "program_hash": program_hash,
                "inputs_digest": inputs_digest,
                "job_execution_id": uid,
                "snapshot": blob,
                "cut": metadata,
            }
            payload = pickle.dumps(envelope, protocol=5)
        except Exception as exc:  # noqa: BLE001
            self.finished(requestid, "failed", {"reason": "capture_invalid", "error": str(exc)})
            return
        row.update(status="persisting", job_execution_id=uid, cut=copy.deepcopy(metadata))
        store, key = self.m.store, checkpoint_key(requestid)
        work = _Work(
            lambda: store.put(key, payload),
            lambda value, error: self._persisted(requestid, error),
        )
        row["work"] = work
        if not self._enqueue(work):
            self.finished(requestid, "failed", {"reason": "io_queue_full"})

    def _persisted(self, token, error):
        row = self._requests.get(token)
        if row is None:
            return
        row["work"] = None
        status = "persist_failed" if error is not None else "persisted"
        if error is None:
            self.persisted += 1
        if row["future"].done():
            row["persistence_status"] = status
            if error is not None:
                row["persistence_error"] = error
            self._requests.pop(token)
            self._requests[token] = row
            self._prune_history()
            return
        self.finished(token, status, {"error": error} if error is not None else {})

    def delete(self, checkpoint_id):
        key = checkpoint_key(checkpoint_id)
        future = Future()
        row = self._requests.get(checkpoint_id)
        if row is not None and (
            not row["future"].done()
            or (row["status"] == "persistence_unknown" and "persistence_status" not in row)
        ):
            future.set_result({"status": "busy", "checkpoint_id": checkpoint_id})
            return future
        store = self.m.store
        if store is None or self._closed:
            future.set_result({"status": "failed", "checkpoint_id": checkpoint_id})
            return future
        operation = uuid.uuid4().hex

        def remove():
            if callable(getattr(store, "delete", None)):
                store.delete(key)
            else:
                store.put(key, None)

        def acknowledge(value, error):
            self._deletes.pop(operation, None)
            if not future.done():
                future.set_result(
                    {
                        "status": "delete_failed" if error else "deleted",
                        "checkpoint_id": checkpoint_id,
                    }
                )

        work = _Work(remove, acknowledge)
        self._deletes[operation] = (work, future, checkpoint_id)
        if not self._enqueue(work):
            acknowledge(None, "io_queue_full")
        else:
            self._schedule(
                time.monotonic() + DEFAULT_TIMEOUT_S, lambda: self._delete_timeout(operation)
            )
        return future

    def _delete_timeout(self, operation):
        pending = self._deletes.get(operation)
        if pending is None:
            return
        work, future, token = pending
        if not future.done():
            entered = work.cancel()
            future.set_result(
                {"status": "persistence_unknown" if entered else "timeout", "checkpoint_id": token}
            )
            if not entered:
                self._deletes.pop(operation, None)

    # Explicit resume stages bytes off-thread, then rejoins the ordinary admission path.
    def prepare_resume(self, item, handle, callback):
        identity = id(item)
        if identity in self._staged:
            return False
        token = getattr(item, "checkpoint", None)
        if token is None:
            return False
        if identity in self._resumes:
            # A source may yield the same object twice; each admission still needs a result.
            separate = SourceItem(item.key, dict(item.inputs), item.entry, item.auto_key, token)
            return self.prepare_resume(separate, handle, callback)
        generation = next(self._sequence)
        pending = {
            "item": item,
            "handle": handle,
            "callback": callback,
            "token": token,
            "generation": generation,
        }
        self._resumes[identity] = pending
        self.pending_resumes += 1
        try:
            key = checkpoint_key(token)
            original_digest, weak = digest_with_weak(item.inputs)
            if weak:
                raise ValueError("explicit resume requires a strong input digest")
            pending.update(key=item.key, entry=item.entry, inputs_digest=original_digest)
            if self.m.store is None or self._closed:
                raise ValueError("checkpoint store is unavailable")
        except Exception as exc:  # noqa: BLE001
            self._resume_loaded(identity, None, str(exc))
            return True
        store = self.m.store
        work = _Work(
            lambda: store.get(key),
            lambda value, error: self._resume_loaded(identity, value, error, generation),
        )
        pending["work"] = work
        if not self._enqueue(work):
            self._resume_loaded(identity, None, "checkpoint IO queue is full")
        else:
            self._schedule(
                time.monotonic() + DEFAULT_TIMEOUT_S,
                lambda: self._resume_timeout(identity, generation),
            )
        return True

    def _resume_timeout(self, identity, generation=None):
        pending = self._resumes.get(identity)
        if pending is not None and (generation is None or pending["generation"] == generation):
            pending["work"].cancel()
            self._resume_loaded(identity, None, "checkpoint read timed out")

    def _resume_loaded(self, identity, value, error, generation=None):
        pending = self._resumes.get(identity)
        if pending is None or (generation is not None and pending["generation"] != generation):
            return
        del self._resumes[identity]
        item = pending["item"]
        try:
            if error is not None:
                raise ValueError(error)
            if value is MISS or value is None:
                raise ValueError("checkpoint artifact not found")
            if type(value) is not bytes:
                raise ValueError("checkpoint artifact must be bytes")
            envelope = pickle.loads(value)
            expected = {
                "schema",
                "version",
                "checkpoint_id",
                "job_key",
                "entry",
                "program_hash",
                "inputs_digest",
                "job_execution_id",
                "snapshot",
                "cut",
            }
            if type(envelope) is not dict or set(envelope) != expected:
                raise ValueError("invalid checkpoint envelope")
            if (
                envelope["schema"] != SCHEMA
                or type(envelope["version"]) is not int
                or envelope["version"] != VERSION
                or envelope["checkpoint_id"] != pending["token"]
            ):
                raise ValueError("checkpoint schema/version/token mismatch")
            if type(envelope["entry"]) is not str or type(envelope["snapshot"]) is not bytes:
                raise ValueError("invalid checkpoint entry or snapshot")
            pending["envelope"] = envelope
            if item.entry is None:
                item.entry = envelope["entry"]
        except Exception as exc:  # noqa: BLE001
            pending["error"] = str(exc)
        self._staged[identity] = pending
        try:
            pending["callback"](item)
        finally:
            self._staged.pop(identity, None)
            self.pending_resumes -= 1

    def discard_resume(self, item):
        self._staged.pop(id(item), None)

    def validate_resume(self, original_item, transformed_item):
        """After pre_admit: return snapshot preflight DTO, denial, or None for a fresh job."""
        pending = self._staged.pop(id(original_item), None)
        if pending is None:
            if (
                getattr(original_item, "checkpoint", None) is not None
                or getattr(transformed_item, "checkpoint", None) is not None
            ):
                raise CheckpointResumeError(
                    "checkpoint_unprepared", "explicit checkpoint was not staged"
                )
            return None
        try:
            if "error" in pending:
                raise ValueError(pending["error"])
            envelope = pending["envelope"]
            if transformed_item.checkpoint != pending["token"]:
                raise ValueError("admission changed or removed the checkpoint token")
            if transformed_item.key != pending["key"] or envelope["job_key"] != pending["key"]:
                raise ValueError("checkpoint job key does not match original admission")
            entry_name = transformed_item.entry
            if entry_name != envelope["entry"] or pending["entry"] not in (None, entry_name):
                raise ValueError("checkpoint entry does not match original admission")
            input_digest, weak = digest_with_weak(transformed_item.inputs)
            if (
                weak
                or input_digest != pending["inputs_digest"]
                or input_digest != envelope["inputs_digest"]
            ):
                raise ValueError("checkpoint inputs do not match original admission")
            entry = self.m.code.entries.get(entry_name)
            if entry is None or entry.region.semantic_hash != envelope["program_hash"]:
                raise ValueError("checkpoint semantic program does not match loaded entry")
            observation = observe_execution(entry.region, self.m.code, self.m)
            prepared = prepare_restore(
                envelope["snapshot"],
                entry,
                self.m.code,
                observation["manifest"],
                self.m.persistence_compatibility,
            )
            if prepared["decision"]["allow"]:
                if prepared["state"]["job_execution_id"] != envelope["job_execution_id"]:
                    raise ValueError("checkpoint UID does not match snapshot UID")
                prepared["entry"] = entry_name
            return prepared
        except Exception as exc:
            raise CheckpointResumeError("checkpoint_incompatible", str(exc)) from exc

    def close(self):
        """Never join a store call: stores may block forever, workers are bounded daemons."""
        if self._closed:
            return
        self._closed = True
        with self._condition:
            self._timers.clear()
            self._condition.notify_all()
        for row in list(self._requests.values()):
            if not row["future"].done():
                work = row["work"]
                entered = work.cancel() if work is not None else False
                self.finished(
                    row["checkpoint_id"],
                    "persistence_unknown" if entered else "cancelled",
                    {"reason": "machine_stopped"},
                )
                self._cancel_cut(row["checkpoint_id"])
        for work, future, token in self._deletes.values():
            entered = work.cancel()
            if not future.done():
                future.set_result(
                    {
                        "status": "persistence_unknown" if entered else "cancelled",
                        "checkpoint_id": token,
                    }
                )
        for pending in list(self._resumes.values()):
            if "work" in pending:
                pending["work"].cancel()
            handle = pending["handle"]
            if handle is None:
                # Source jobs must still reach the source run's sink through normal rejection.
                self._resume_loaded(id(pending["item"]), None, "machine stopped during resume")
            else:
                item = pending["item"]
                handle._set(
                    JobRecord(
                        item.key,
                        item.entry or self.m.default_entry,
                        False,
                        failure={
                            "kind": "Cancelled",
                            "node": None,
                            "message": "machine stopped during resume",
                        },
                    )
                )
        self._resumes.clear()
        self._staged.clear()
        self.pending_resumes = 0
        while True:
            try:
                work = self._queue.get_nowait()
                if work is not None:
                    work.cancel()
            except queue.Empty:
                break
        for _ in range(IO_WORKERS):
            self._queue.put_nowait(None)
