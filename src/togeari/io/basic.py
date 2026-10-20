"""Sources and sinks that need nothing but the standard library."""

import glob
import json
from collections.abc import Iterable as _Iterable
from pathlib import Path
from typing import Any

from togeari.core.contracts.io import JobRecord, SourceItem


class Iterable:
    """Jobs from an iterable of dicts (or SourceItems); keys are positions unless ``key`` is set."""

    def __init__(self, items: _Iterable, entry: str | None = None, key: str | None = None):
        self.items = items
        self.entry = entry
        self.key = key
        self.rank, self.world = 0, 1

    def shard(self, rank: int, world: int) -> "Iterable":
        s = Iterable(self.items, self.entry, self.key)
        s.rank, s.world = rank, world
        return s

    def __iter__(self):
        for i, it in enumerate(self.items):
            if i % self.world != self.rank:
                continue
            if isinstance(it, SourceItem):
                yield it
                continue
            if self.key:
                yield SourceItem(str(it[self.key]), dict(it), self.entry)
            else:
                yield SourceItem(str(i), dict(it), self.entry, auto_key=True)


class Jsonl:
    """Read jobs from / write records to a JSON-lines file."""

    def __init__(self, path: str | Path, entry: str | None = None, key: str | None = None):
        self.path = Path(path)
        self.entry = entry
        self.key = key
        self.rank, self.world = 0, 1
        self._f = None

    def shard(self, rank: int, world: int) -> "Jsonl":
        s = Jsonl(self.path, self.entry, self.key)
        s.rank, s.world = rank, world
        return s

    def __iter__(self):
        with self.path.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip() or i % self.world != self.rank:
                    continue
                obj = json.loads(line)
                entry = obj.pop("__entry__", self.entry)
                if "__key__" in obj:
                    yield SourceItem(str(obj.pop("__key__")), obj, entry)
                elif self.key:
                    yield SourceItem(str(obj.get(self.key)), obj, entry)
                else:
                    yield SourceItem(str(i), obj, entry, auto_key=True)

    def write(self, record: JobRecord) -> None:
        if self._f is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._f = self.path.open("a", encoding="utf-8")
        row = {
            "key": record.key,
            "entry": record.entry,
            "ok": record.ok,
            "outputs": record.outputs,
            "failure": record.failure,
            "missing": record.missing,
        }
        self._f.write(json.dumps(row, default=repr) + "\n")
        self._f.flush()

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


class Folder:
    """One job per file matching a glob; the path goes into ``field``."""

    def __init__(self, pattern: str, field: str = "path", entry: str | None = None):
        self.pattern = pattern
        self.field = field
        self.entry = entry
        self.rank, self.world = 0, 1

    def shard(self, rank: int, world: int) -> "Folder":
        s = Folder(self.pattern, self.field, self.entry)
        s.rank, s.world = rank, world
        return s

    def __iter__(self):
        for i, p in enumerate(sorted(glob.glob(self.pattern, recursive=True))):
            if i % self.world == self.rank:
                yield SourceItem(p, {self.field: p}, self.entry)


class Memory:
    """Keep every record in a list (and by key)."""

    def __init__(self) -> None:
        self.records: list[JobRecord] = []

    def write(self, record: JobRecord) -> None:
        self.records.append(record)

    def close(self) -> None:
        return None

    def by_key(self) -> dict[str, JobRecord]:
        return {r.key: r for r in self.records}

    def outputs(self) -> dict[str, Any]:
        return {r.key: r.outputs for r in self.records}
