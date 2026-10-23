"""Measure resident event history and synchronous checkpoint costs, not delivery backlog.

Quick run: python scripts/benches/event_history.py --out event-history.json
Each sample uses a fresh process, a disk journal/store, and a nonretaining sink.
No timing threshold is asserted. psutil supplies process RSS (install it with uv).
"""

import argparse
import gc
import importlib
import importlib.metadata
import json
import os
import platform
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path

import psutil
from kohakunode import NodeLibrary

from togeari import Inline, Machine, Use
from togeari.core.contracts.engine import EngineBase
from togeari.core.contracts.events import EventOutput
from togeari.core.contracts.io import MISS
from togeari.core.contracts.results import Ok
from togeari.core.runtime.retire import checkpoint, codec
from togeari.io.events import SQLiteEventJournal

LIB = NodeLibrary("event_history_bench")


@LIB.node()
def gate(value: int) -> int:
    return value


PROGRAM = """@def (n: int, payload: str)main(result):
    (n)range(items)
    (items)each(i, `body`)
    body:
        ("sample", payload)emit()
    (n)gate(prefix)
    (prefix)gate(result)
@def ()independent(result):
    result = 1
"""


class Controlled(EngineBase):
    def __init__(self):
        super().__init__("measurement-gate")
        self.submissions = queue.Queue()
        self.ticket = 0

    def capacity(self):
        return 2

    def submit(self, item, done):
        self.ticket += 1
        self.submissions.put((self.ticket, item.inputs["value"], done))
        return self.ticket


class CountingSink:
    def write_event(self, event):
        pass

    def close(self):
        pass


class MeasuredJournal(SQLiteEventJournal):
    def __init__(self, path):
        super().__init__(path)
        self.condition = threading.Condition()
        self.acks = 0

    def ack(self, event_id):
        super().ack(event_id)
        with self.condition:
            self.acks += 1
            self.condition.notify_all()

    def drained(self, count):
        with self.condition:
            if not self.condition.wait_for(lambda: self.acks >= count, timeout=120):
                raise TimeoutError("delivery did not drain")


class DiskStore:
    def __init__(self, root):
        self.root = root
        self.put_start = self.put_end = None
        self.artifact_bytes = 0

    def get(self, key):
        path = self.root / key.hex()
        return path.read_bytes() if path.exists() else MISS

    def put(self, key, value):
        self.put_start = time.perf_counter()
        self.artifact_bytes = len(value)
        with (self.root / key.hex()).open("wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        self.put_end = time.perf_counter()

    def delete(self, key):
        (self.root / key.hex()).unlink(missing_ok=True)


def retained_size(value, seen=None):
    """Reachable Python object sizes, deduplicated; deliberately excludes native allocations."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        size += sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(retained_size(v, seen) for v in value)
    return size


def sample(core, count, payload_bytes, record):
    timings, history = {}, {}
    fast = {}
    fast_created = threading.Event()
    original_encode = codec.encode_snapshot

    # Local instrumentation: native Host captures this callable at Machine construction.
    def encode(*args):
        timings["independent_enqueued"] = time.perf_counter()
        fast["handle"] = machine.submit({}, entry="independent", key="independent")
        fast_created.set()
        timings["encode_start"] = time.perf_counter()
        result = original_encode(*args)
        timings["encode_end"] = time.perf_counter()
        timings["snapshot_bytes"] = len(result)
        return result

    codec.encode_snapshot = checkpoint.encode_snapshot = encode
    tracemalloc.start()
    process = psutil.Process()
    with tempfile.TemporaryDirectory(prefix="togeari-event-history-") as folder:
        root = Path(folder)
        journal, store, engine = (
            MeasuredJournal(root / "journal.sqlite"),
            DiskStore(root),
            Controlled(),
        )
        machine = Machine(
            programs={PROGRAM: ["main", "independent"]},
            entry="main",
            libraries=[LIB],
            bind={"gate": Use(engine, binding_revision="gate/1"), "kir.std.*": Inline()},
            core=core,
            record=record,
            store=store,
            event_output=EventOutput(
                CountingSink(),
                journal,
                max_events=16,
                max_bytes=1 << 20,
                sink_revision="discard/1",
            ),
        )
        original_capture = machine.checkpoints.captured

        def captured(*args):
            timings["manager_capture_start"] = time.perf_counter()
            original_capture(*args)
            timings["manager_capture_end"] = time.perf_counter()

        machine.checkpoints.captured = captured
        try:
            machine.submit({}, entry="independent", key="warmup").result(30)
            gc.collect()
            baseline_rss = process.memory_info().rss
            baseline_python = tracemalloc.get_traced_memory()[0]
            machine.submit({"n": count, "payload": "x" * payload_bytes}, key="resident")
            ticket, value, done = engine.submissions.get(timeout=120)
            journal.drained(count)
            # The inbox barrier lets the final acknowledgment release the outbox reservation.
            output = machine.inspect("event_output").result(30)["status"]
            while output["pending_events"]:
                output = machine.inspect("event_output").result(30)["status"]
            [job] = machine.inspect("jobs").result(30)

            def measure():
                stats = machine.dispatcher.jobs[job["id"]].stats
                events = stats["committed_events"]["events"]
                history.update(events=len(events), python_bytes=retained_size(events))

            measured = threading.Event()

            def measure_and_signal():
                try:
                    measure()
                finally:
                    measured.set()

            machine.dispatcher.inbox.put(("call", measure_and_signal))
            if not measured.wait(30):
                raise TimeoutError("resident measurement did not run")
            gc.collect()
            rss = process.memory_info().rss
            python_current, python_peak = tracemalloc.get_traced_memory()
            request_start = time.perf_counter()
            reply = machine.command("checkpoint_job", id=job["id"], timeout_s=120)
            machine.inspect("checkpoints", job=job["id"]).result(30)
            # Timestamp completion independently, rather than after storage acknowledgment.
            fast_done = threading.Event()

            def await_fast():
                if not fast_created.wait(120):
                    raise TimeoutError("capture did not enqueue independent work")
                fast["handle"].result(120)
                timings["independent_done"] = time.perf_counter()
                fast_done.set()

            observer = threading.Thread(target=await_fast, daemon=True)
            observer.start()
            done(ticket, [Ok(value, True)])
            saved = reply.result(120)
            persisted = time.perf_counter()
            if saved["status"] != "persisted" or not fast_done.wait(120):
                raise RuntimeError(f"checkpoint/independent job did not finish: {saved}")
            observer.join()
            assert history["events"] == count
            return {
                "core": core,
                "event_count": count,
                "payload_bytes": payload_bytes,
                "record": record,
                "history_python_bytes": history["python_bytes"],
                "process_rss_baseline": baseline_rss,
                "process_rss_resident": rss,
                "process_rss_delta": rss - baseline_rss,
                "traced_python_delta": python_current - baseline_python,
                "traced_python_peak": python_peak,
                "outbox_pending_events": output["pending_events"],
                "snapshot_bytes": timings["snapshot_bytes"],
                "artifact_bytes": store.artifact_bytes,
                "encode_ms": 1000 * (timings["encode_end"] - timings["encode_start"]),
                "manager_capture_ms": 1000
                * (timings["manager_capture_end"] - timings["manager_capture_start"]),
                "independent_delay_ms": 1000
                * (timings["independent_done"] - timings["independent_enqueued"]),
                "store_write_fsync_ms": 1000 * (store.put_end - store.put_start),
                "checkpoint_total_ms": 1000 * (persisted - request_start),
                "post_encode_to_persisted_ms": 1000 * (persisted - timings["encode_end"]),
                "knobs": {"max_events": 16, "max_bytes": 1 << 20},
            }
        finally:
            machine.stop(drain=False, timeout=5)
            journal.close()
            codec.encode_snapshot = checkpoint.encode_snapshot = original_encode
            tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("event-history.json"))
    parser.add_argument("--counts", type=int, nargs="+", default=[128, 512, 2048])
    parser.add_argument("--payloads", type=int, nargs="+", default=[256, 4096])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument(
        "--cores", nargs="+", choices=["python", "rust"], default=["python", "rust"]
    )
    parser.add_argument("--record", choices=["none", "all"], default="none")
    parser.add_argument("--large", action="store_true", help="allow >8 MiB raw payload per sample")
    parser.add_argument("--sample", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.counts + args.payloads + [args.repeats]) < 1:
        parser.error("counts, payload sizes and repeats must be positive")
    if max(args.counts) * max(args.payloads) > 8 << 20 and not args.large:
        parser.error("quick preset caps raw retained payload at 8 MiB; pass --large explicitly")
    if args.sample:
        print(
            json.dumps(
                sample(
                    args.cores[0],
                    args.counts[0],
                    args.payloads[0],
                    None if args.record == "none" else 1.0,
                )
            )
        )
        return
    rows = []
    for core in args.cores:
        for payload in args.payloads:
            for count in args.counts:
                for repeat in range(args.repeats):
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--sample",
                        "--cores",
                        core,
                        "--counts",
                        str(count),
                        "--payloads",
                        str(payload),
                        "--record",
                        args.record,
                    ]
                    if args.large:
                        command.append("--large")
                    row = json.loads(subprocess.check_output(command, text=True))
                    row["repeat"] = repeat
                    rows.append(row)
                    print(core, count, payload, repeat, "encode_ms", round(row["encode_ms"], 3))
    summaries = []
    for core in args.cores:
        for payload in args.payloads:
            for count in args.counts:
                group = [
                    r
                    for r in rows
                    if (r["core"], r["payload_bytes"], r["event_count"]) == (core, payload, count)
                ]
                summaries.append(
                    {
                        "core": core,
                        "payload_bytes": payload,
                        "event_count": count,
                        **{
                            key: statistics.median(r[key] for r in group)
                            for key in (
                                "history_python_bytes",
                                "artifact_bytes",
                                "encode_ms",
                                "manager_capture_ms",
                                "independent_delay_ms",
                                "store_write_fsync_ms",
                                "checkpoint_total_ms",
                                "process_rss_delta",
                            )
                        },
                    }
                )
    # Record checkout identities and exact binaries, not just release version strings.
    packages = {}
    for name in ("kohakunode", "togeari"):
        package = importlib.import_module(name)
        native = importlib.import_module(name + "._rs")
        root = Path(package.__file__).resolve().parents[2]
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain"], text=True
        )
        packages[name] = {
            "revision": revision,
            "working_tree_changes": dirty.splitlines(),
            "source": package.__file__,
            "extension": native.__file__,
        }
    report = {
        "packages": packages,
        "method": (
            "fresh subprocess/sample; resident gate; delivery drained; "
            "benchmark-local codec/capture wrappers"
        ),
        "python": sys.version,
        "platform": platform.platform(),
        "versions": {n: importlib.metadata.version(n) for n in ("kohakunode", "togeari", "psutil")},
        "journal": "SQLite WAL FULL; disk-backed",
        "store": "file write+fsync; temporary local disk",
        "memory_coverage": (
            "history reachable Python objects; tracemalloc Python only; "
            "RSS whole process including native and SQLite"
        ),
        "timing_coverage": (
            "encode and manager packing synchronous; independent delay includes "
            "scheduler/observer overhead; storage fsync separately; tracemalloc enabled"
        ),
        "samples": rows,
        "medians": summaries,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
