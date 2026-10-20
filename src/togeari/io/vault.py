"""A KohakuVault database as a source of jobs and as a sink of records (extra ``[vault]``)."""

import dataclasses
import pickle
from pathlib import Path

from kohakuvault import KVault

from togeari.core.contracts.io import JobRecord, SourceItem


class Vault:
    """Source: every key / value of the database is a job; sink: pickled records by job key.

    As a source, a value is unpickled; a dict becomes the job's inputs, any
    other value goes into ``field``. Keys are decoded as UTF-8.
    """

    def __init__(self, path: str | Path, entry: str | None = None, field: str = "value"):
        self.path = Path(path)
        self.entry = entry
        self.field = field
        self.rank, self.world = 0, 1
        self._kv: KVault | None = None

    def shard(self, rank: int, world: int) -> "Vault":
        s = Vault(self.path, self.entry, self.field)
        s.rank, s.world = rank, world
        return s

    @staticmethod
    def _open(path: Path) -> KVault:
        path.parent.mkdir(parents=True, exist_ok=True)
        kv = KVault(str(path))
        kv.disable_auto_pack()
        return kv

    def __iter__(self):
        kv = self._open(self.path)
        try:
            for i, (key, data) in enumerate(kv.items()):
                if i % self.world != self.rank:
                    continue
                value = pickle.loads(data)
                inputs = value if isinstance(value, dict) else {self.field: value}
                yield SourceItem(key.decode("utf-8", "replace"), inputs, self.entry)
        finally:
            kv.close()

    def write(self, record: JobRecord) -> None:
        """The record without its conformance record (that one goes to the recorder)."""
        if self._kv is None:
            self._kv = self._open(self.path)
        self._kv[record.key] = pickle.dumps(dataclasses.replace(record, record=None))

    def close(self) -> None:
        if self._kv is not None:
            self._kv.close()
            self._kv = None

    def records(self) -> dict[str, JobRecord]:
        """Every record written to the database, by job key."""
        kv = self._open(self.path)
        try:
            return {k.decode("utf-8", "replace"): pickle.loads(v) for k, v in kv.items()}
        finally:
            kv.close()
