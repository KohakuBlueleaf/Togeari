"""Conformance records on disk: one entry per recorded job, plus the programs to replay them.

Layout of a record directory:

- ``programs/<semantic hash>.json``: one program: source, KIR text, entry
  ``@def``, passes, hash, library references (``module:attr``) and signature
  paths; one file per program, so writers of several runs never overwrite
  each other;
- ``runs/<run>/run.json``: the run's id, start time and ``digest_providers``
  (modules that register digest functions, imported before a replay so input
  digests are computed as in the run);
- ``runs/<run>/record.kv`` (KohakuVault, when installed): job key -> pickled
  job entry; ``runs/<run>/record.pkl`` otherwise: the pickled job entries one
  after another.

A job entry is a dict with ``run``, ``key``, ``entry``, ``program`` (the
semantic hash of the program it ran), ``inputs``, ``ok``, ``outputs``,
``failure``, ``missing``, ``stats``, ``record`` (the machine's call records in
retire order) and ``weak`` (values stored by ``repr`` because they do not
pickle). Every run writes its own files, and every job names its program by
hash, so a job replays against the program it ran.
"""

import importlib
import json
import logging
import os
import pickle
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from togeari.core.contracts.digest import digest_providers
from togeari.plugins.common.worker import Worker, left

log = logging.getLogger("togeari.plugins")

try:
    from kohakuvault import KVault
except ImportError:
    KVault = None

PROGRAMS = "programs"
RUNS = "runs"
RUN_INFO = "run.json"
RECORD_KV = "record.kv"
RECORD_PKL = "record.pkl"


def library_refs(libraries: list) -> list[str]:
    """``module:attr`` of each library object: the earliest imported module holding it.

    ``__main__`` and the CLI's ``togeari_machine_*`` names are used only when no other
    module holds the object.
    """
    refs = []
    for lib in libraries:
        found = []
        for mod_name, mod in list(sys.modules.items()):
            for attr, value in list(getattr(mod, "__dict__", {}).items()):
                if value is lib and not attr.startswith("_"):
                    found.append(f"{mod_name}:{attr}")
                    break
        found.sort(key=lambda r: r.startswith(("__main__:", "togeari_machine_")))
        refs.append(found[0] if found else f"<unresolved {type(lib).__name__}>")
    return refs


def program_info(job_program: Any, libraries: list[str], sig_path: list | None) -> dict:
    return {
        "source": job_program.source,
        "entry": job_program.entry,
        "text": job_program.text,
        "passes": list(job_program.passes),
        "hash": job_program.semantic_hash,
        "libraries": list(libraries),
        "sig_path": [str(p) for p in sig_path or []],
    }


def import_providers(modules: list[str]) -> list[str]:
    """Import each digest provider module; returns the ones that failed, with the error."""
    failed = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    return failed


def _write_json(path: Path, data: Any, tmp_tag: str) -> None:
    """Write ``data`` to ``path`` through a temporary file and an atomic replace."""
    tmp = path.with_name(f"{path.name}.{tmp_tag}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def run_id() -> str:
    """A new run's id: start time, process id and a random suffix."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(3)}"


def _picklable(v: Any, weak: list, where: str) -> Any:
    try:
        pickle.dumps(v)
        return v
    except Exception:  # noqa: BLE001
        weak.append(where)
        return repr(v)


def job_entry(record: Any, inputs: Any, run: str, program: str | None) -> tuple[bytes, list]:
    """The pickled job entry and its weak values: values that do not pickle are stored by
    ``repr`` and listed."""
    stats = {k: v for k, v in record.stats.items() if k != "record"}
    entry = {
        "run": run,
        "key": record.key,
        "entry": record.entry,
        "program": program,
        "inputs": inputs,
        "ok": record.ok,
        "outputs": record.outputs,
        "failure": record.failure,
        "missing": list(record.missing),
        "stats": stats,
        "record": list(record.record or []),
        "weak": [],
    }
    try:
        return pickle.dumps(entry), []
    except Exception:  # noqa: BLE001
        pass

    # Value by value: every unpicklable value becomes its repr
    weak: list[str] = []
    entry["inputs"] = {k: _picklable(v, weak, f"inputs.{k}") for k, v in (inputs or {}).items()}
    entry["outputs"] = {
        k: _picklable(v, weak, f"outputs.{k}") for k, v in (record.outputs or {}).items()
    }
    entry["stats"] = {k: _picklable(v, weak, f"stats.{k}") for k, v in stats.items()}
    calls = []
    for i, (node, dig, kind, value, mutated) in enumerate(entry["record"]):
        value = _picklable(value, weak, f"record.{i}.{node}")
        if mutated is not None:
            mutated = {p: _picklable(v, weak, f"record.{i}.{node}.{p}") for p, v in mutated.items()}
        calls.append((node, dig, kind, value, mutated))
    entry["record"] = calls
    entry["weak"] = weak
    return pickle.dumps(entry), weak


class _Records:
    """One run's job entries: KohakuVault when installed, else a pickle stream."""

    def __init__(self, folder: Path, append: bool):
        self.kv = None
        self.pkl = None
        if KVault is not None and (append or (folder / RECORD_KV).exists()):
            self.kv = KVault(str(folder / RECORD_KV))
            self.kv.disable_auto_pack()
        else:
            self.pkl = (folder / RECORD_PKL).open("ab" if append else "wb")

    def put(self, key: str, data: bytes) -> None:
        if self.kv is not None:
            self.kv[key] = data
        else:
            self.pkl.write(data)
            self.pkl.flush()

    def close(self) -> None:
        if self.kv is not None:
            self.kv.close()
        if self.pkl is not None:
            self.pkl.close()


class ArchiveWriter:
    """Writes one run's job entries and the programs they ran, on its own thread.

    The writer's queue holds at most ``queue_size`` tasks; tasks past it are
    dropped and counted in ``dropped``. ``weak_jobs`` counts written jobs with weak
    values, ``weak_values`` those values. ``close(timeout)`` drains for at most about
    twice ``timeout``; after it the files are closed and tasks still queued write nothing.
    """

    def __init__(self, path: str | Path, queue_size: int = 100_000):
        self.path = Path(path)
        self.run = run_id()
        self.folder = self.path / RUNS / self.run
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.path / PROGRAMS).mkdir(exist_ok=True)
        self.info = {"run": self.run, "started": time.time(), "digest_providers": []}
        self.set_providers(digest_providers())
        self.records: _Records | None = None
        self.written = 0
        self.weak_jobs = 0
        self.weak_values = 0

        # Writes and close take this lock; once closed, a task writes nothing
        self._io = threading.Lock()
        self._closed = False
        self.worker = Worker(f"togeari-archive-{self.run}", self._handle, maxsize=queue_size)

    @property
    def dropped(self) -> int:
        return self.worker.dropped

    def set_providers(self, providers: list[str]) -> None:
        """Record the run's digest providers in ``run.json`` (written now, by the caller)."""
        self.info["digest_providers"] = sorted(providers)
        _write_json(self.folder / RUN_INFO, self.info, self.run)

    def add_program(self, info: dict) -> None:
        self.worker.put(("program", info))

    def add_providers(self, providers: list[str]) -> None:
        self.worker.put(("providers", providers))

    def add_job(self, record: Any, inputs: Any, program: str | None) -> None:
        self.worker.put(("job", record, inputs, program))

    def _handle(self, task: tuple) -> None:
        if self._closed:
            return
        # A job's entry is built, and the record files opened (seconds on a cold disk), outside
        # the lock: close waits for a write, never for those
        opened = None
        if task[0] == "job":
            _, record, inputs, program = task
            loaded = pickle.loads(inputs) if inputs is not None else None
            data, weak = job_entry(record, loaded, self.run, program)
            task = ("entry", record.key, data, weak)
            if self.records is None:
                opened = _Records(self.folder, append=True)
        with self._io:
            if self._closed:
                if opened is not None:
                    opened.close()
                return
            if opened is not None:
                self.records = opened
            self._write(task)

    def _write(self, task: tuple) -> None:
        match task:
            case ("program", info):
                _write_json(self.path / PROGRAMS / f"{info['hash']}.json", info, self.run)
            case ("providers", providers):
                self.set_providers(providers)
            case ("entry", key, data, weak):
                self.records.put(key, data)
                self.written += 1
                if weak:
                    self.weak_jobs += 1
                    self.weak_values += len(weak)

    def close(self, timeout: float = 10.0) -> None:
        """Drain the writer, close the record files, and end the writer, all within
        ``timeout``; the task the writer is in the middle of then writes nothing (files it
        is opening it closes itself)."""
        deadline = time.monotonic() + timeout
        self.worker.close(timeout=timeout)

        # The record files close; a writer still running skips its remaining tasks
        overran = self.worker.thread.is_alive()
        self.worker.abandon()
        with self._io:
            self._closed = True
            if self.records is not None:
                self.records.close()
                self.records = None
        if overran:
            log.warning("archive writer did not finish in %ss; queued records dropped", timeout)
            self.worker.thread.join(timeout=left(deadline))


class Archive:
    """A record directory, read back: every run, or the runs named in ``runs``."""

    def __init__(self, path: str | Path, runs: list[str] | None = None):
        self.path = Path(path)
        programs = self.path / PROGRAMS
        if not programs.is_dir():
            raise FileNotFoundError(f"{programs} not found; not a record directory")
        self.programs: dict[str, dict] = {}
        for file in sorted(programs.glob("*.json")):
            info = json.loads(file.read_text(encoding="utf-8"))
            self.programs[info["hash"]] = info
        self.only = runs

    def runs(self) -> list[str]:
        """Run ids, oldest first."""
        folder = self.path / RUNS
        names = sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []
        return [n for n in names if self.only is None or n in self.only]

    def run_info(self, run: str) -> dict:
        """A run's ``run.json`` (empty when it is missing)."""
        file = self.path / RUNS / run / RUN_INFO
        return json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}

    def jobs(self) -> Iterator[dict]:
        for run in self.runs():
            yield from self._run_jobs(self.path / RUNS / run)

    def _run_jobs(self, folder: Path) -> Iterator[dict]:
        kv_path, pkl_path = folder / RECORD_KV, folder / RECORD_PKL
        if kv_path.exists():
            if KVault is None:
                raise RuntimeError(f"{kv_path} needs kohakuvault (pip install togeari[vault])")
            kv = KVault(str(kv_path))
            kv.disable_auto_pack()
            try:
                for value in kv.values():
                    yield pickle.loads(value)
            finally:
                kv.close()
        if pkl_path.exists():
            with pkl_path.open("rb") as f:
                while True:
                    try:
                        yield pickle.load(f)
                    except EOFError:
                        break

    def rewrite(self, entries: list[dict]) -> None:
        """Replace the given job entries, matched by run and key (tools and tests)."""
        by_run: dict[str, dict[str, dict]] = {}
        for e in entries:
            by_run.setdefault(e["run"], {})[e["key"]] = e
        for run, changed in by_run.items():
            folder = self.path / RUNS / run
            if (folder / RECORD_KV).exists():
                records = _Records(folder, append=True)
                for key, e in changed.items():
                    records.put(key, pickle.dumps(e))
            else:
                kept = [changed.get(e["key"], e) for e in self._run_jobs(folder)]
                records = _Records(folder, append=False)
                for e in kept:
                    records.put(e["key"], pickle.dumps(e))
            records.close()
